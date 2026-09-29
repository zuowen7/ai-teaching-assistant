"""RAG (Retrieval-Augmented Generation) routes.

Endpoints:
  GET    /api/rag/documents              — list all ingested docs
  POST   /api/rag/ingest                 — ingest text by JSON
  POST   /api/rag/upload                 — upload file and ingest
  DELETE /api/rag/documents/{doc_id}     — delete a doc
  POST   /api/rag/query                  — semantic search

Two indexing modes share the collection: the legacy flat-text mode (translation
auto-ingest, uploads, manual text) and the page-aware evidence mode used by
project literature sources, which stores page/character/artifact metadata so a
hit can be resolved back to a real page quote.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.literature.evidence import (
    CHUNKER_VERSION,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_VERSION,
    INDEX_VERSION,
    PARSER_VERSION,
    build_page_chunks,
    chunk_metadata,
    index_fingerprint,
)

logger = logging.getLogger(__name__)
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_CHUNK_TARGET_CHARS = 1400
_CHUNK_OVERLAP_CHARS = 180
_PAGE_DOC_KIND = "literature_pages"


class IngestRequest(BaseModel):
    doc_id: str | None = None
    title: str | None = None
    text: str = Field(min_length=1, max_length=1_000_000)
    project_root: str | None = Field(default=None, max_length=2000)
    source_id: str | None = Field(default=None, max_length=128)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    top_k: int = Field(default=5, ge=1, le=50)
    project_root: str | None = Field(default=None, max_length=2000)
    source_ids: list[str] | None = Field(default=None, max_length=100)
    project_scoped: bool = False


def build_translation_doc_id(source_text: str) -> str:
    """翻译自动入库的稳定 doc_id。

    基于源文本内容哈希生成：同一篇文献重复翻译会命中同一 doc_id，
    配合 delete-then-upsert 实现去重，避免向量库出现重复条目。
    """
    import hashlib

    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
    return f"trans_{digest}"


_MAX_CHUNKS_PER_DOC = 2


def _dedupe_hits(
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any] | None],
    distances: list[float | None],
    top_k: int,
    max_chunks_per_doc: int = _MAX_CHUNKS_PER_DOC,
) -> list[dict[str, Any]]:
    """按文档去重检索结果。

    每篇文档最多贡献 max_chunks_per_doc 个 chunk（按相关性顺序保留最靠前的），
    最终返回至多 top_k 条，避免单篇文献霸占全部结果。
    """
    hits: list[dict[str, Any]] = []
    per_doc_count: dict[str, int] = {}
    for index, chunk_id in enumerate(ids):
        metadata = metadatas[index] if index < len(metadatas) and metadatas[index] else {}
        doc_id = metadata.get("doc_id", chunk_id)
        if per_doc_count.get(doc_id, 0) >= max_chunks_per_doc:
            continue
        per_doc_count[doc_id] = per_doc_count.get(doc_id, 0) + 1
        hits.append(
            {
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "source": metadata.get("title", chunk_id),
                "text": documents[index] if index < len(documents) else "",
                "distance": distances[index] if index < len(distances) else None,
                "metadata": metadata,
            }
        )
        if len(hits) >= top_k:
            break
    return hits


def _chunk_text(
    text: str,
    *,
    target_chars: int = _CHUNK_TARGET_CHARS,
    overlap_chars: int = _CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split extracted prose into stable retrieval chunks without losing paragraph context."""
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n\n")]
    paragraphs = [part for part in paragraphs if part]
    if not paragraphs:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [
            paragraph[index : index + target_chars]
            for index in range(0, len(paragraph), target_chars)
        ] or [paragraph]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > target_chars:
                chunks.append(current)
                prefix = current[-overlap_chars:].lstrip()
                current = f"{prefix}\n\n{piece}".strip()
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def register_rag_routes(
    app: FastAPI,
    *,
    runtime_dir: Path,
) -> dict[str, Any]:
    """Register RAG endpoints. Returns state dict with get/ensure helpers."""
    _docs: dict[str, dict] = {}
    _chroma_client = None
    _collection = None
    _data_dir = runtime_dir / "data" / "chromadb"
    _docs_path = _data_dir / "documents.json"
    _store_lock = asyncio.Lock()
    _operation_lock = asyncio.Lock()

    if _docs_path.is_file():
        try:
            raw_docs = json.loads(_docs_path.read_text(encoding="utf-8"))
            if isinstance(raw_docs, dict):
                _docs.update({str(k): v for k, v in raw_docs.items() if isinstance(v, dict)})
        except (OSError, json.JSONDecodeError):
            logger.warning("RAG metadata index could not be loaded", exc_info=True)

    def _save_docs() -> None:
        _data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=_data_dir, prefix=".documents.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_docs, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, _docs_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _get_store():
        return _collection

    async def _ensure_store():
        nonlocal _chroma_client, _collection
        if _collection is not None:
            return _collection
        async with _store_lock:
            if _collection is not None:
                return _collection
            try:
                import chromadb

                def _open_store():
                    _data_dir.mkdir(parents=True, exist_ok=True)
                    client = chromadb.PersistentClient(path=str(_data_dir))
                    return client, client.get_or_create_collection("documents")

                _chroma_client, _collection = await asyncio.to_thread(_open_store)
            except Exception as e:
                logger.warning("RAG store init failed: %s", e)
        return _collection

    state: dict[str, Any] = {
        "get_rag_store": _get_store,
        "ensure_rag_store": _ensure_store,
    }

    @app.get("/api/rag/documents")
    async def rag_list_documents(project_root: str | None = None):
        documents = list(_docs.values())
        if project_root is not None:
            documents = [item for item in documents if item.get("project_root") == project_root]
        return documents

    async def _ingest(
        *,
        doc_id: str,
        title: str,
        text: str,
        project_root: str | None = None,
        source_id: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        chunks = _chunk_text(text)
        if not chunks:
            raise HTTPException(422, "Document has no extractable text")
        entry = {
            "doc_id": doc_id,
            "title": title,
            "text_length": len(text),
            "chunk_count": len(chunks),
            "project_root": project_root,
            "source_id": source_id,
            "filename": filename,
        }
        col = await _ensure_store()
        if col is None:
            raise HTTPException(503, "RAG store not available")
        ids = [f"{doc_id}::{index}" for index in range(len(chunks))]
        metadatas = []
        for index in range(len(chunks)):
            metadata: dict[str, str | int] = {
                "doc_id": doc_id,
                "title": title,
                "chunk_index": index,
            }
            if project_root is not None:
                metadata["project_root"] = project_root
            if source_id is not None:
                metadata["source_id"] = source_id
            metadatas.append(metadata)
        try:
            async with _operation_lock:
                # Remove stale chunks when a source is re-indexed with different boundaries.
                await asyncio.to_thread(col.delete, where={"doc_id": doc_id})
                await asyncio.to_thread(
                    col.upsert,
                    ids=ids,
                    documents=chunks,
                    metadatas=metadatas,
                )
                _docs[doc_id] = entry
                await asyncio.to_thread(_save_docs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("RAG ingest to chromadb failed: %s", exc)
            raise HTTPException(500, "RAG document ingest failed")
        return entry

    async def _embedding_identity() -> tuple[str, str]:
        """Best-effort identity of the collection's embedding function.

        ChromaDB is used without an explicit ``embedding_function``, so the
        honest record is the function the library actually installed (its class
        name) plus an explicitly unpinned version marker.  The store is opened
        first on purpose: reading the identity before the collection exists
        returns the fallback on the very first call and the real class name
        afterwards, which would make the index fingerprint depend on call order
        and mark a freshly built index as stale.
        """

        function = getattr(await _ensure_store(), "_embedding_function", None)
        name = type(function).__name__ if function is not None else ""
        if not name or name == "NoneType":
            return DEFAULT_EMBEDDING_MODEL, DEFAULT_EMBEDDING_VERSION
        return name, DEFAULT_EMBEDDING_VERSION

    async def _index_pages(
        *,
        doc_id: str,
        title: str,
        pages: list[tuple[int, str]],
        artifact_sha256: str,
        project_root: str | None = None,
        source_id: str | None = None,
        filename: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Index page-scoped chunks that carry full evidence metadata.

        Re-indexing is idempotent: an existing entry with the same artifact hash
        and index fingerprint is reused unless ``force`` is set.
        """

        embedding_model, embedding_version = await _embedding_identity()
        fingerprint = index_fingerprint(
            artifact_sha256=artifact_sha256,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            index_version=INDEX_VERSION,
        )
        existing = _docs.get(doc_id)
        if (
            not force
            and isinstance(existing, dict)
            and existing.get("index_fingerprint") == fingerprint
            and existing.get("artifact_sha256") == artifact_sha256
        ):
            return {**existing, "reused": True}

        chunks = build_page_chunks(pages, artifact_sha256=artifact_sha256)
        if not chunks:
            raise HTTPException(422, "Document has no indexable page text")
        col = await _ensure_store()
        if col is None:
            raise HTTPException(503, "RAG store not available")

        ids = [chunk.chunk_id for chunk in chunks]
        documents = [chunk.text for chunk in chunks]
        metadatas = [
            chunk_metadata(
                chunk,
                doc_id=doc_id,
                title=title,
                chunk_index=index,
                artifact_sha256=artifact_sha256,
                index_fingerprint_value=fingerprint,
                project_root=project_root,
                source_id=source_id,
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_model=embedding_model,
                embedding_version=embedding_version,
                index_version=INDEX_VERSION,
            )
            for index, chunk in enumerate(chunks)
        ]
        entry: dict[str, Any] = {
            "doc_id": doc_id,
            "title": title,
            "kind": _PAGE_DOC_KIND,
            "chunk_count": len(chunks),
            "page_count": len({chunk.page_number for chunk in chunks}),
            "char_count": sum(len(chunk.text) for chunk in chunks),
            "artifact_sha256": artifact_sha256,
            "index_fingerprint": fingerprint,
            "parser_version": PARSER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": embedding_model,
            "embedding_version": embedding_version,
            "index_version": INDEX_VERSION,
            "indexed_at": datetime.now(UTC).isoformat(),
            "filename": filename,
            "project_root": project_root,
            "source_id": source_id,
        }
        try:
            async with _operation_lock:
                await asyncio.to_thread(col.delete, where={"doc_id": doc_id})
                await asyncio.to_thread(
                    col.upsert,
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                )
                _docs[doc_id] = entry
                await asyncio.to_thread(_save_docs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("RAG page indexing failed: %s", exc)
            raise HTTPException(500, "RAG page indexing failed")
        return {**entry, "reused": False}

    async def _get_chunk(chunk_id: str) -> dict[str, Any] | None:
        """Fetch one stored chunk together with its evidence metadata."""

        col = await _ensure_store()
        if col is None:
            raise HTTPException(503, "RAG store not available")
        try:
            result = await asyncio.to_thread(
                col.get,
                ids=[chunk_id],
                include=["documents", "metadatas"],
            )
        except Exception as exc:
            logger.warning("RAG chunk lookup failed: %s", exc)
            raise HTTPException(500, "RAG chunk lookup failed")
        ids = result.get("ids") or []
        if not ids:
            return None
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        return {
            "chunk_id": str(ids[0]),
            "text": str(documents[0]) if documents else "",
            "metadata": dict(metadatas[0] or {}) if metadatas else {},
        }

    @app.post("/api/rag/ingest")
    async def rag_ingest(req: IngestRequest):
        doc_id = req.doc_id or f"doc_{uuid.uuid4().hex[:8]}"
        entry = await _ingest(
            doc_id=doc_id,
            title=req.title or doc_id,
            text=req.text,
            project_root=req.project_root,
            source_id=req.source_id,
        )
        return {
            "status": "ok",
            "doc_id": doc_id,
            "chunk_count": entry["chunk_count"],
        }

    @app.post("/api/rag/upload")
    async def rag_upload(file: UploadFile):
        from src.parser import SUPPORTED_EXTENSIONS, extract_document

        content = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Uploaded document is too large (max 20 MB)")
        filename = Path(file.filename or "source").name
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, f"Unsupported document format: {extension or 'unknown'}")
        if not content:
            raise HTTPException(422, "Uploaded document is empty")
        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(suffix=extension)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            document = await asyncio.to_thread(extract_document, temp_path)
            text = document.full_text.strip()
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("RAG upload parse failed for %s: %s", filename, exc)
            raise HTTPException(422, "Uploaded document could not be parsed")
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
        if not text:
            raise HTTPException(422, "Uploaded document has no extractable text")
        doc_id = f"upload_{uuid.uuid4().hex[:8]}"
        entry = await _ingest(
            doc_id=doc_id,
            title=filename,
            text=text,
            filename=filename,
        )
        return {
            "status": "ok",
            "doc_id": doc_id,
            "filename": filename,
            "chunk_count": entry["chunk_count"],
        }

    @app.delete("/api/rag/documents/{doc_id}")
    async def rag_delete_document(doc_id: str):
        if doc_id not in _docs:
            raise HTTPException(404, f"Document {doc_id} not found")
        await _delete_document(doc_id)
        return {"status": "ok", "deleted": doc_id}

    async def _delete_document(doc_id: str) -> None:
        col = await _ensure_store()
        if col is None:
            raise HTTPException(503, "RAG store not available")
        try:
            async with _operation_lock:
                await asyncio.to_thread(col.delete, where={"doc_id": doc_id})
                # Compatibility with documents indexed before chunking was introduced.
                await asyncio.to_thread(col.delete, ids=[doc_id])
                _docs.pop(doc_id, None)
                await asyncio.to_thread(_save_docs)
        except Exception as e:
            logger.warning("RAG delete failed: %s", e)
            raise HTTPException(500, "RAG document delete failed")

    async def _query_pages(
        *,
        query: str,
        top_k: int,
        project_root: str | None = None,
        source_ids: Sequence[str] | None = None,
        project_scoped: bool = False,
    ) -> list[dict[str, Any]]:
        """Run one scoped vector query and return de-duplicated hits.

        The HTTP route and the P3 evidence answer service share this function so
        that "project and source scoped" has exactly one implementation.
        """

        if project_scoped and (not project_root or not source_ids):
            raise HTTPException(
                400,
                "project_scoped queries require project_root and explicit source_ids",
            )
        col = await _ensure_store()
        if col is None:
            raise HTTPException(503, "RAG store not available")
        try:
            where: dict[str, Any] | None = None
            filters: list[dict[str, Any]] = []
            if project_root is not None:
                filters.append({"project_root": project_root})
            if source_ids:
                filters.append({"source_id": {"$in": list(source_ids)}})
            if len(filters) == 1:
                where = filters[0]
            elif filters:
                where = {"$and": filters}
            query_kwargs: dict[str, Any] = {
                "query_texts": [query],
                # 多取候选，便于按文档去重后仍能凑满 top_k
                "n_results": min(max(top_k * 3, 10), 50),
            }
            if where is not None:
                query_kwargs["where"] = where
            results = await asyncio.to_thread(
                col.query,
                **query_kwargs,
            )
            ids = (results.get("ids") or [[]])[0]
            documents = (results.get("documents") or [[]])[0]
            metadatas = (results.get("metadatas") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]
            return _dedupe_hits(ids, documents, metadatas, distances, top_k)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("RAG query failed: %s", exc)
            raise HTTPException(500, "RAG query failed") from exc

    @app.post("/api/rag/query")
    async def rag_query(req: QueryRequest):
        return {
            "hits": await _query_pages(
                query=req.query,
                top_k=req.top_k,
                project_root=req.project_root,
                source_ids=req.source_ids,
                project_scoped=req.project_scoped,
            )
        }

    state.update(
        {
            "index_pages": _index_pages,
            "get_chunk": _get_chunk,
            "get_document": lambda doc_id: _docs.get(doc_id),
            "delete_document": _delete_document,
            "embedding_identity": _embedding_identity,
            "query_pages": _query_pages,
        }
    )
    return state
