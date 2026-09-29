"""P3 integration test: scoped retrieval to a verifiable answer on a real store.

Scope (frozen in docs/literature-research-poc-plan.md section 5.9): with the real
ChromaDB-backed store, a real PDF and the shipping ``RagPageRetriever``, one
scoped question must produce claims whose evidence resolves back to the exact
slice of the normalized page text — and must never cite a source outside the
requested scope.

Only the answer model is substituted; retrieval, persistence, scope filters and
evidence resolution are the production code path.  Offline; skipped explicitly
when chromadb is unavailable.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from routers.literature import RagPageIndexStore, RagPageRetriever  # noqa: E402
from routers.rag import register_rag_routes  # noqa: E402
from src.literature.answer import AnswerStatus  # noqa: E402
from src.literature.answer_model import build_model_identity  # noqa: E402
from src.literature.evidence import sha256_file  # noqa: E402
from src.literature.service import LiteratureService  # noqa: E402
from tests.unit.test_literature_indexing import (  # noqa: E402
    PAGE_TWO_MARKER,
    PROJECT,
    make_indexed_source,
    parsed_pages,
)
from tests.unit.test_literature_service import (  # noqa: E402
    StaticProvider,
    make_record,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)
QUESTION = "What does the study say about the different section?"
OTHER_SOURCE_ID = "src_other00000000000001"
EVIDENCE_ID_RE = re.compile(r"\[(evidence_[0-9a-f]{24})\]")


class ScriptedModel:
    """Deterministic stand-in for the answering model."""

    def __init__(self) -> None:
        self.identity = build_model_identity(provider="scripted", model="scripted-answer-v1")
        self.prompts: list[str] = []

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "claims": [
                    {
                        "text": "The selected paper reports this result.",
                        "evidence_ids": EVIDENCE_ID_RE.findall(prompt),
                        "evidence_status": "supported",
                    }
                ]
            }
        )


@pytest.fixture()
def rag_state():
    pytest.importorskip("chromadb")
    runtime_dir = Path(tempfile.mkdtemp(prefix="p3-answer-rag-"))
    app = FastAPI()
    state = register_rag_routes(app, runtime_dir=runtime_dir)
    try:
        yield state
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


async def test_scoped_answer_cites_a_real_page_quote(tmp_path: Path, rag_state) -> None:
    """The whole P3 chain, with only the model substituted."""

    _, store, _memory_index, source_id, pdf = await make_indexed_source(tmp_path)
    pages = parsed_pages(pdf)
    artifact_sha256 = sha256_file(pdf)

    await rag_state["index_pages"](
        doc_id=f"project:{source_id}",
        title="Alpha Paper",
        pages=sorted(pages.items()),
        artifact_sha256=artifact_sha256,
        project_root=PROJECT,
        source_id=source_id,
        filename=pdf.name,
    )
    # A second source in the same project and collection must stay out of scope.
    await rag_state["index_pages"](
        doc_id=f"project:{OTHER_SOURCE_ID}",
        title="Other Paper",
        pages=sorted(pages.items()),
        artifact_sha256=artifact_sha256,
        project_root=PROJECT,
        source_id=OTHER_SOURCE_ID,
        filename="other.pdf",
    )

    # Regression (D-032): identical bytes must not make one document overwrite the
    # other's page metadata, so both sources stay independently retrievable.
    for scoped_source in (source_id, OTHER_SOURCE_ID):
        scoped_hits = await rag_state["query_pages"](
            query=QUESTION,
            top_k=5,
            project_root=PROJECT,
            source_ids=[scoped_source],
            project_scoped=True,
        )
        assert scoped_hits, f"{scoped_source} lost its page chunks to a colliding document"
        assert {hit["metadata"]["source_id"] for hit in scoped_hits} == {scoped_source}
        assert all(hit["chunk_id"].startswith("chunk_") for hit in scoped_hits)

    model = ScriptedModel()
    service = LiteratureService(
        providers=[StaticProvider("alpha", [make_record("alpha", "2401.00001")])],
        project_store=store,
        index_store=RagPageIndexStore(rag_state),
        retriever=RagPageRetriever(rag_state),
        answer_model=model,
        now_factory=lambda: NOW,
    )

    result = await service.answer_question(
        project_path=PROJECT,
        question=QUESTION,
        source_ids=[source_id],
        top_k=5,
    )

    assert result.status is AnswerStatus.ANSWERED
    assert result.claims
    assert result.evidence
    assert model.prompts
    assert QUESTION in model.prompts[0]

    # Scope: nothing outside the requested source may be cited.
    assert {item.source_id for item in result.evidence} == {source_id}
    assert {item.source_id for item in result.evidence} <= set(result.source_ids)

    # Every claim cites evidence that is present and machine-resolvable.
    evidence_ids = {item.span.evidence_id for item in result.evidence}
    for claim in result.claims:
        assert set(claim.evidence_ids) <= evidence_ids

    # Each quote equals the exact slice of the re-parsed normalized page text.
    for item in result.evidence:
        span = item.span
        assert span.artifact_sha256 == artifact_sha256
        assert span.page_start == span.page_end
        assert pages[span.page_start][span.char_start : span.char_end] == span.exact_quote
        assert span.coordinate_space == "normalized_page_text_v1"

    # Retrieval really worked: the queried phrase is on page two.
    assert any(PAGE_TWO_MARKER in item.span.exact_quote for item in result.evidence)
    assert any(item.span.page_start == 2 for item in result.evidence)


async def test_unanswerable_scope_returns_insufficient_without_a_model_call(
    tmp_path: Path, rag_state
) -> None:
    """An empty scope is refused; an unindexed scope answers insufficient."""

    _, store, _memory_index, source_id, pdf = await make_indexed_source(tmp_path)
    model = ScriptedModel()
    service = LiteratureService(
        providers=[StaticProvider("alpha", [make_record("alpha", "2401.00001")])],
        project_store=store,
        index_store=RagPageIndexStore(rag_state),
        retriever=RagPageRetriever(rag_state),
        answer_model=model,
        now_factory=lambda: NOW,
    )

    # Nothing was indexed into the real store for this source.
    result = await service.answer_question(
        project_path=PROJECT,
        question=QUESTION,
        source_ids=[source_id],
        top_k=5,
    )

    assert result.status is AnswerStatus.INSUFFICIENT
    assert result.insufficient_reason is not None
    assert result.claims == []
    assert result.evidence == []
    assert model.prompts == []
    assert sha256_file(pdf)
