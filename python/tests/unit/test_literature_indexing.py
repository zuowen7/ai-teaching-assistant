"""P2B tests: page-aware indexing and machine-checkable evidence resolution.

The stage gate of docs/literature-research-poc-plan.md section 5 is
"every retrieval hit must resolve back to the real page and exact quote of the
same hashed document", so the main test here indexes a real multi-page PDF and
resolves *every* stored chunk individually.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.literature.evidence import (
    CHUNKER_VERSION,
    INDEX_VERSION,
    PARSER_VERSION,
    build_page_chunks,
    chunk_metadata,
    index_fingerprint,
    normalize_page_text,
    sha256_file,
)
from src.literature.models import FullTextStatus
from src.literature.service import (
    LiteratureService,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
)
from tests.unit.test_literature_service import (
    NOW,
    MemoryProjectStore,
    StaticProvider,
    make_record,
    search,
)

PROJECT = "D:/projects/indexing"
PAGE_ONE = (
    "Page one reports the first result of the study.\n\n"
    "Its closing paragraph must never continue onto the following page."
)
PAGE_TWO = (
    "Page two opens a different section of the same paper.\n\n"
    "Only this page may host quotes drawn from its own text."
)
PAGE_TWO_MARKER = "different section"


def write_pdf(path: Path, page_texts: Sequence[str]) -> Path:
    """Write a real, extractable multi-page PDF with PyMuPDF."""

    import fitz

    document = fitz.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 96), text, fontsize=11)
    document.save(str(path))
    document.close()
    return path


def parsed_pages(path: Path) -> dict[int, str]:
    """Independently re-parse the artifact the way evidence coordinates expect."""

    from src.parser import extract_document

    document = extract_document(path)
    return {
        page.page_num: normalize_page_text(page.text)
        for page in document.pages
        if normalize_page_text(page.text)
    }


class MemoryPageIndexStore:
    """In-memory page index that mirrors the metadata contract of routers/rag.py."""

    def __init__(
        self,
        *,
        embedding_model: str = "chromadb-default",
        embedding_version: str = "unpinned",
    ) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.chunks: dict[str, dict[str, Any]] = {}
        self.index_calls = 0
        self._embedding = (embedding_model, embedding_version)

    async def embedding_identity(self) -> tuple[str, str]:
        return self._embedding

    async def index_pages(
        self,
        *,
        doc_id: str,
        title: str,
        pages: Sequence[tuple[int, str]],
        artifact_sha256: str,
        project_root: str | None = None,
        source_id: str | None = None,
        filename: str | None = None,
        force: bool = False,
    ) -> Mapping[str, Any]:
        self.index_calls += 1
        fingerprint = index_fingerprint(
            artifact_sha256=artifact_sha256,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=self._embedding[0],
            embedding_version=self._embedding[1],
            index_version=INDEX_VERSION,
        )
        existing = self.documents.get(doc_id)
        if (
            not force
            and existing is not None
            and existing.get("index_fingerprint") == fingerprint
            and existing.get("artifact_sha256") == artifact_sha256
        ):
            return {**existing, "reused": True}

        chunks = build_page_chunks(list(pages), artifact_sha256=artifact_sha256)
        for chunk_id in [key for key, value in self.chunks.items() if value["doc_id"] == doc_id]:
            del self.chunks[chunk_id]
        for index, chunk in enumerate(chunks):
            metadata = chunk_metadata(
                chunk,
                doc_id=doc_id,
                title=title,
                chunk_index=index,
                artifact_sha256=artifact_sha256,
                index_fingerprint_value=fingerprint,
                project_root=project_root,
                source_id=source_id,
            )
            self.chunks[chunk.chunk_id] = {
                "chunk_id": chunk.chunk_id,
                "doc_id": doc_id,
                "text": chunk.text,
                "metadata": metadata,
            }
        entry = {
            "doc_id": doc_id,
            "title": title,
            "kind": "literature_pages",
            "chunk_count": len(chunks),
            "page_count": len({chunk.page_number for chunk in chunks}),
            "artifact_sha256": artifact_sha256,
            "index_fingerprint": fingerprint,
            "parser_version": PARSER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": self._embedding[0],
            "embedding_version": self._embedding[1],
            "index_version": INDEX_VERSION,
            "indexed_at": NOW.isoformat(),
        }
        self.documents[doc_id] = entry
        return {**entry, "reused": False}

    async def get_chunk(self, chunk_id: str) -> Mapping[str, Any] | None:
        return self.chunks.get(chunk_id)

    async def get_document(self, doc_id: str) -> Mapping[str, Any] | None:
        return self.documents.get(doc_id)

    def chunk_ids_on_page(self, page_number: int) -> list[str]:
        return sorted(
            key
            for key, value in self.chunks.items()
            if value["metadata"].get("page_start") == page_number
        )


async def make_indexed_source(
    tmp_path: Path,
    *,
    page_texts: Sequence[str] = (PAGE_ONE, PAGE_TWO),
    pdf_name: str = "paper.pdf",
):
    """Import one literature source and point it at a real local PDF."""

    store = MemoryProjectStore()
    index_store = MemoryPageIndexStore()
    provider = StaticProvider("alpha", [make_record("alpha", "2401.00001")])
    service = LiteratureService(
        providers=[provider],
        project_store=store,
        index_store=index_store,
        now_factory=lambda: NOW,
        source_id_factory=lambda: "src_lit_indexing0001",
    )
    execution = await search(service, "alpha")
    batch = await service.import_selection(
        project_path=PROJECT,
        search_execution_id=execution.search_execution_id,
        paper_ids=[provider.records[0].paper_id],
    )
    source_id = batch.results[0].source_id
    pdf = write_pdf(tmp_path / pdf_name, page_texts)
    store.artifacts[source_id] = str(pdf)
    return service, store, index_store, source_id, pdf


async def test_every_indexed_chunk_resolves_to_its_own_page_and_quote(tmp_path: Path) -> None:
    """Stage gate: every retrieval hit resolves to a real page and exact quote."""

    service, store, index_store, source_id, pdf = await make_indexed_source(tmp_path)
    record = StaticProvider("alpha", [make_record("alpha", "2401.00001")]).records[0]

    result = await service.index_source(project_path=PROJECT, source_id=source_id)

    assert result.status is FullTextStatus.INDEXED
    assert result.artifact_sha256 == sha256_file(pdf)
    assert result.page_count == 2
    assert result.chunk_count == len(index_store.chunks) >= 2
    stored_pages = {value["metadata"]["page_start"] for value in index_store.chunks.values()}
    assert stored_pages == {1, 2}

    expected_pages = parsed_pages(pdf)
    resolved_ids: list[str] = []
    for chunk_id, stored in index_store.chunks.items():
        resolved = await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )
        span = resolved.span
        resolved_ids.append(chunk_id)

        assert span.page_start == span.page_end == stored["metadata"]["page_start"]
        assert span.artifact_sha256 == result.artifact_sha256
        assert span.coordinate_space == "normalized_page_text_v1"
        assert span.evidence_id and span.quote_sha256
        assert span.exact_quote == stored["text"]
        assert expected_pages[span.page_start][span.char_start : span.char_end] == span.exact_quote
        assert span.exact_quote.strip()
        assert resolved.paper_id == record.paper_id
        assert resolved.source_id == source_id
        if span.page_start == 1:
            assert PAGE_TWO_MARKER not in span.exact_quote
        else:
            assert PAGE_TWO_MARKER in expected_pages[2]
    assert len(resolved_ids) == len(index_store.chunks)


async def test_index_persists_fulltext_and_index_metadata(tmp_path: Path) -> None:
    service, store, index_store, source_id, pdf = await make_indexed_source(tmp_path)

    result = await service.index_source(project_path=PROJECT, source_id=source_id)

    source = store.read_sources(PROJECT)[0]
    literature = source["metadata"]["literature"]
    fulltext = literature["fulltext"]
    index = literature["index"]

    assert fulltext["status"] == "indexed"
    assert fulltext["sha256"] == sha256_file(pdf)
    assert fulltext["local_path"] == str(pdf)
    assert fulltext["file_size_bytes"] == pdf.stat().st_size
    assert fulltext["mime_type"] == "application/pdf"
    assert fulltext["artifact_id"].startswith("artifact_")
    assert fulltext["failure_reason"] is None
    assert index["doc_id"] == f"project:{source_id}"
    assert index["chunk_count"] == result.chunk_count
    assert index["page_count"] == 2
    assert index["artifact_sha256"] == result.artifact_sha256
    assert index["index_fingerprint"] == result.index_fingerprint
    assert index["parser_version"] == PARSER_VERSION
    assert index["chunker_version"] == CHUNKER_VERSION
    assert index["index_version"] == INDEX_VERSION


async def test_reindexing_is_idempotent_and_force_rebuilds(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)

    first = await service.index_source(project_path=PROJECT, source_id=source_id)
    second = await service.index_source(project_path=PROJECT, source_id=source_id)
    forced = await service.index_source(project_path=PROJECT, source_id=source_id, force=True)

    assert first.reused is False
    assert second.reused is True
    assert forced.reused is False
    assert second.chunk_count == first.chunk_count == forced.chunk_count
    assert index_store.index_calls == 3


async def test_replaced_pdf_invalidates_the_index_instead_of_quoting_it(tmp_path: Path) -> None:
    service, store, index_store, source_id, pdf = await make_indexed_source(tmp_path)
    await service.index_source(project_path=PROJECT, source_id=source_id)
    chunk_id = next(iter(index_store.chunks))

    write_pdf(pdf, ["A completely different replacement document.", "Second page too."])

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )

    assert captured.value.code is LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED
    assert captured.value.details["code"] == "artifact_hash_mismatch"


async def test_reindexing_after_pdf_replacement_rebuilds_and_drops_old_chunks(
    tmp_path: Path,
) -> None:
    """A replaced artifact must produce a rebuilt index, not a reused one."""

    service, store, index_store, source_id, pdf = await make_indexed_source(tmp_path)
    first = await service.index_source(project_path=PROJECT, source_id=source_id)
    stale_chunk_id = next(iter(index_store.chunks))

    write_pdf(pdf, ["Replacement page one text.", "Replacement page two text."])
    second = await service.index_source(project_path=PROJECT, source_id=source_id)

    assert second.reused is False
    assert second.artifact_sha256 != first.artifact_sha256
    assert second.artifact_sha256 == sha256_file(pdf)
    assert stale_chunk_id not in index_store.chunks

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=stale_chunk_id,
        )
    assert captured.value.code is LiteratureServiceErrorCode.CHUNK_NOT_FOUND

    for chunk_id, stored in index_store.chunks.items():
        resolved = await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )
        assert resolved.span.artifact_sha256 == second.artifact_sha256
        assert resolved.span.exact_quote == stored["text"]

    literature = store.read_sources(PROJECT)[0]["metadata"]["literature"]
    assert literature["fulltext"]["sha256"] == second.artifact_sha256
    assert literature["index"]["artifact_sha256"] == second.artifact_sha256


async def test_version_change_marks_the_index_stale(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    await service.index_source(project_path=PROJECT, source_id=source_id)
    chunk_id = next(iter(index_store.chunks))
    index_store.chunks[chunk_id]["metadata"]["chunker_version"] = "page-chunker-v0"

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )

    assert captured.value.code is LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED
    assert captured.value.details["code"] == "stale_index"


async def test_embedding_change_marks_the_index_stale(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    await service.index_source(project_path=PROJECT, source_id=source_id)
    chunk_id = next(iter(index_store.chunks))

    index_store._embedding = ("a-different-embedding-model", "unpinned")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )

    assert captured.value.details["code"] == "stale_index"


async def test_page_less_chunk_can_never_become_evidence(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    index_store.chunks["trans_chunk_1"] = {
        "chunk_id": "trans_chunk_1",
        "doc_id": "trans_deadbeef",
        "text": "Translation text without any page coordinates.",
        "metadata": {
            "doc_id": "trans_deadbeef",
            "title": "[翻译] paper.pdf",
            "chunk_index": 0,
            "source_kind": "translation",
        },
    }

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id="trans_chunk_1",
        )

    assert captured.value.code is LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED
    assert captured.value.details["code"] == "missing_page_metadata"


async def test_chunk_from_another_source_is_refused(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    await service.index_source(project_path=PROJECT, source_id=source_id)
    chunk_id = next(iter(index_store.chunks))
    index_store.chunks[chunk_id]["metadata"]["source_id"] = "src_lit_other"

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id=chunk_id,
        )

    assert captured.value.code is LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED
    assert "不属于该项目文献" in str(captured.value)


async def test_missing_chunk_is_an_explicit_failure(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.resolve_evidence(
            project_path=PROJECT,
            source_id=source_id,
            chunk_id="chunk_missing",
        )

    assert captured.value.code is LiteratureServiceErrorCode.CHUNK_NOT_FOUND


async def test_non_pdf_artifact_is_not_evidence_indexable(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    plain = tmp_path / "notes.docx"
    plain.write_bytes(b"not really a docx")
    store.artifacts[source_id] = str(plain)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.UNSUPPORTED_EVIDENCE_FORMAT
    assert index_store.index_calls == 0


async def test_unreadable_pdf_records_a_visible_parse_failure(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not a real pdf")
    store.artifacts[source_id] = str(broken)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.PARSE_FAILED
    fulltext = store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "parse_failed"
    assert fulltext["failure_reason"]
    assert fulltext["sha256"] == sha256_file(broken)


async def test_pdf_without_extractable_text_is_a_parse_failure(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    empty = write_pdf(tmp_path / "empty.pdf", ["", ""])
    store.artifacts[source_id] = str(empty)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.PARSE_FAILED
    assert index_store.index_calls == 0


async def test_missing_artifact_is_distinct_from_a_parse_failure(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    store.artifacts.pop(source_id)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.SOURCE_ARTIFACT_MISSING


async def test_indexing_is_refused_without_an_index_store(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    service._index_store = None

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.INDEX_STORE_UNAVAILABLE


async def test_plain_project_source_is_not_part_of_the_evidence_workflow(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)
    sources = store.read_sources(PROJECT)
    del sources[0]["metadata"]["literature"]
    store.projects[PROJECT] = deepcopy(sources)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.SOURCE_NOT_LITERATURE


async def test_unknown_source_is_an_explicit_failure(tmp_path: Path) -> None:
    service, store, index_store, source_id, _pdf = await make_indexed_source(tmp_path)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.index_source(project_path=PROJECT, source_id="src_lit_unknown")

    assert captured.value.code is LiteratureServiceErrorCode.SOURCE_NOT_FOUND
