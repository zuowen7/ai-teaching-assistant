"""P2B integration tests against the real ChromaDB-backed RAG store.

Scope (frozen in docs/literature-research-poc-plan.md section 5.7): page-level
metadata must survive a real persistent write/read cycle, page numbers must stay
integers, chunk lookup must round-trip, rebuilds must remove stale chunks, and a
``project_scoped`` query must only return page-level chunks inside the requested
project and sources — never the flat translation chunks that share the same
collection.  A hit read back from the real store must still resolve into an
evidence span.

These tests are offline.  They require a working chromadb installation; when the
dependency is unavailable they skip explicitly instead of pretending to pass.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from routers.rag import register_rag_routes  # noqa: E402
from src.literature.evidence import (  # noqa: E402
    build_page_chunks,
    chunk_metadata,
    index_fingerprint,
    normalize_page_text,
    resolve_evidence_span,
)

pytestmark = pytest.mark.integration

PAGE_ONE = (
    "Retrieval augmented generation grounds answers in retrieved passages.\n\n"
    "This first page states the problem statement of the study."
)
PAGE_TWO = (
    "The second page reports the evaluation protocol and its metrics.\n\n"
    "Only this page mentions the held-out evaluation split."
)
REPLACED_PAGE_TWO = (
    "The second page was replaced after a new revision of the document.\n\n"
    "It no longer mentions the held-out evaluation split."
)
ARTIFACT_SHA = "b" * 64
REPLACED_ARTIFACT_SHA = "c" * 64
DOC_ID = "project:src_lit_integration"
SOURCE_ID = "src_lit_integration"
PROJECT_ROOT = "D:/projects/integration"


@pytest.fixture()
def rag_app():
    pytest.importorskip("chromadb")
    # A private directory instead of tmp_path: chromadb keeps file handles open,
    # so pytest's own temp cleanup can fail on Windows.  Removal is best effort.
    runtime_dir = Path(tempfile.mkdtemp(prefix="p2b-rag-"))
    app = FastAPI()
    state = register_rag_routes(app, runtime_dir=runtime_dir)
    try:
        yield app, state
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


async def index_pages(
    state,
    *,
    pages: Sequence[tuple[int, str]],
    artifact_sha256: str = ARTIFACT_SHA,
    doc_id: str = DOC_ID,
    force: bool = False,
    source_id: str = SOURCE_ID,
):
    return await state["index_pages"](
        doc_id=doc_id,
        title="Integration paper",
        pages=list(pages),
        artifact_sha256=artifact_sha256,
        project_root=PROJECT_ROOT,
        source_id=source_id,
        filename="integration.pdf",
        force=force,
    )


async def test_real_store_round_trips_page_metadata(rag_app) -> None:
    _app, state = rag_app
    chunks = build_page_chunks(
        [(1, PAGE_ONE), (2, PAGE_TWO)],
        artifact_sha256=ARTIFACT_SHA,
    )

    entry = await index_pages(state, pages=[(1, PAGE_ONE), (2, PAGE_TWO)])

    assert entry["chunk_count"] == len(chunks)
    assert entry["page_count"] == 2
    assert entry["reused"] is False
    embedding_model, embedding_version = await state["embedding_identity"]()
    assert entry["embedding_model"] == embedding_model
    assert entry["index_fingerprint"] == index_fingerprint(
        artifact_sha256=ARTIFACT_SHA,
        embedding_model=embedding_model,
        embedding_version=embedding_version,
    )
    # The identity must not depend on call order: reading it again after the
    # store has been used must return the same pair.
    assert await state["embedding_identity"]() == (embedding_model, embedding_version)

    page_one_chunk = next(chunk for chunk in chunks if chunk.page_number == 1)
    stored = await state["get_chunk"](page_one_chunk.chunk_id)

    assert stored is not None
    assert stored["text"] == page_one_chunk.text
    metadata = stored["metadata"]
    assert metadata["page_start"] == 1
    assert isinstance(metadata["page_start"], int)
    assert metadata["page_end"] == 1
    assert metadata["char_start"] == page_one_chunk.char_start
    assert metadata["char_end"] == page_one_chunk.char_end
    assert metadata["artifact_sha256"] == ARTIFACT_SHA
    assert metadata["source_id"] == SOURCE_ID
    assert metadata["project_root"] == PROJECT_ROOT
    assert metadata["source_kind"] == "literature_page"

    document = state["get_document"](DOC_ID)
    assert document is not None
    assert document["kind"] == "literature_pages"
    assert document["artifact_sha256"] == ARTIFACT_SHA

    span = resolve_evidence_span(
        source_id=SOURCE_ID,
        artifact_sha256=metadata["artifact_sha256"],
        page_texts={1: normalize_page_text(PAGE_ONE), 2: normalize_page_text(PAGE_TWO)},
        page_number=metadata["page_start"],
        char_start=metadata["char_start"],
        char_end=metadata["char_end"],
        chunk_id=stored["chunk_id"],
        parser_version=metadata["parser_version"],
        chunker_version=metadata["chunker_version"],
        embedding_model=metadata["embedding_model"],
        embedding_version=metadata["embedding_version"],
        index_version=metadata["index_version"],
    )
    assert span.exact_quote == stored["text"]


async def test_reindexing_reuses_then_rebuilds_and_drops_stale_chunks(rag_app) -> None:
    _app, state = rag_app
    first = await index_pages(state, pages=[(1, PAGE_ONE), (2, PAGE_TWO)])
    original_chunks = build_page_chunks(
        [(1, PAGE_ONE), (2, PAGE_TWO)],
        artifact_sha256=ARTIFACT_SHA,
    )
    stale_id = next(chunk.chunk_id for chunk in original_chunks if chunk.page_number == 2)
    assert await state["get_chunk"](stale_id) is not None

    reused = await index_pages(state, pages=[(1, PAGE_ONE), (2, PAGE_TWO)])
    assert reused["reused"] is True
    assert reused["chunk_count"] == first["chunk_count"]

    rebuilt = await index_pages(
        state,
        pages=[(1, PAGE_ONE), (2, REPLACED_PAGE_TWO)],
        artifact_sha256=REPLACED_ARTIFACT_SHA,
    )

    assert rebuilt["reused"] is False
    assert rebuilt["artifact_sha256"] == REPLACED_ARTIFACT_SHA
    assert await state["get_chunk"](stale_id) is None
    new_chunks = build_page_chunks(
        [(1, PAGE_ONE), (2, REPLACED_PAGE_TWO)],
        artifact_sha256=REPLACED_ARTIFACT_SHA,
    )
    stored = await state["get_chunk"](new_chunks[0].chunk_id)
    assert stored is not None
    assert stored["metadata"]["artifact_sha256"] == REPLACED_ARTIFACT_SHA


async def test_scoped_query_returns_only_page_chunks_in_range(rag_app) -> None:
    app, state = rag_app
    await index_pages(state, pages=[(1, PAGE_ONE), (2, PAGE_TWO)])
    await index_pages(
        state,
        pages=[(1, "Another project document about unrelated retrieval.")],
        artifact_sha256="d" * 64,
        doc_id="project:src_lit_other",
        source_id="src_lit_other",
    )

    with TestClient(app) as client:
        flat = client.post(
            "/api/rag/ingest",
            json={
                "doc_id": "trans_flat",
                "title": "[翻译] flat document",
                "text": "Retrieval augmented generation grounds answers in passages. " * 40,
            },
        )
        assert flat.status_code == 200

        scoped = client.post(
            "/api/rag/query",
            json={
                "query": "held-out evaluation split",
                "top_k": 8,
                "project_root": PROJECT_ROOT,
                "source_ids": [SOURCE_ID],
                "project_scoped": True,
            },
        )
        assert scoped.status_code == 200
        hits = scoped.json()["hits"]
        assert hits, "scoped query returned no hits for an indexed document"
        assert all(hit["metadata"]["source_id"] == SOURCE_ID for hit in hits)
        assert all("page_start" in hit["metadata"] for hit in hits)
        assert all(hit["chunk_id"].startswith("chunk_") for hit in hits)
        assert all(hit["doc_id"] == DOC_ID for hit in hits)

        unscoped = client.post(
            "/api/rag/query",
            json={"query": "held-out evaluation split", "project_scoped": True},
        )
        assert unscoped.status_code == 400
        assert "source_ids" in unscoped.json()["detail"]
