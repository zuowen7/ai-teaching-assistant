"""P3 unit tests for the answer service orchestration (plan 5.9).

These tests cover the service-level contract that the pure logic in
``src/literature/answer.py`` cannot express: scope enforcement, retrieval and
evidence resolution order, when the model is *not* called, how a model failure is
reported, and the fact that an answer is never cached.

Everything is offline: the retriever and the answer model are deterministic fakes,
while the page index is an in-memory store driven by a real PDF.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.literature.answer import AnswerStatus, InsufficientReason, RejectedClaimReason
from src.literature.answer_model import build_model_identity
from src.literature.service import (
    LiteratureService,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
    RetrievedChunk,
)
from tests.unit.test_literature_indexing import (
    PAGE_ONE,
    PAGE_TWO,
    PROJECT,
    make_indexed_source,
    parsed_pages,
)
from tests.unit.test_literature_service import StaticProvider, make_record

NOW = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)
QUESTION = "Does the reported effect hold across the selected papers?"
EVIDENCE_ID_RE = re.compile(r"\[(evidence_[0-9a-f]{24})\]")
OTHER_SOURCE = "src_other000000000000"


class FakeRetriever:
    """Deterministic stand-in for the scoped page-level retrieval channel."""

    def __init__(self, hits: Sequence[RetrievedChunk] | None = None) -> None:
        self.hits = list(hits or [])
        self.calls: list[dict] = []
        self.failure: Exception | None = None

    async def retrieve(
        self,
        *,
        project_root: str,
        source_ids: Sequence[str],
        query: str,
        top_k: int,
    ) -> Sequence[RetrievedChunk]:
        self.calls.append(
            {
                "project_root": project_root,
                "source_ids": list(source_ids),
                "query": query,
                "top_k": top_k,
            }
        )
        if self.failure is not None:
            raise self.failure
        return list(self.hits)


class FakeAnswerModel:
    """Deterministic stand-in for the injected evidence answer model."""

    def __init__(
        self,
        *,
        response: str | None = None,
        response_factory=None,
        failure: Exception | None = None,
        provider: str = "openai",
        model: str = "gpt-4o",
    ) -> None:
        self.identity = build_model_identity(provider=provider, model=model)
        self.failure = failure
        self.response = response
        self.response_factory = response_factory
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        if self.failure is not None:
            raise self.failure
        if self.response_factory is not None:
            return self.response_factory(prompt)
        if self.response is not None:
            return self.response
        evidence_ids = EVIDENCE_ID_RE.findall(prompt)
        return json.dumps(
            {
                "claims": [
                    {
                        "text": "The selected corpus reports a consistent result.",
                        "evidence_ids": evidence_ids[:1],
                        "evidence_status": "supported",
                    }
                ]
            }
        )


def answer_claim(evidence_ids: Sequence[str], text: str = "A grounded claim.") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "text": text,
                    "evidence_ids": list(evidence_ids),
                    "evidence_status": "supported",
                }
            ]
        }
    )


async def make_answerable(
    tmp_path: Path,
    *,
    page_texts: Sequence[str] = (PAGE_ONE, PAGE_TWO),
    with_retriever: bool = True,
    with_answer_model: bool = True,
):
    """Index one real two-page PDF, then wire a retriever and an answer model."""

    _, store, index_store, source_id, pdf = await make_indexed_source(
        tmp_path, page_texts=page_texts
    )
    base = LiteratureService(
        providers=[StaticProvider("alpha", [make_record("alpha", "2401.00001")])],
        project_store=store,
        index_store=index_store,
        now_factory=lambda: NOW,
        source_id_factory=lambda: "src_lit_answer00000001",
    )
    await base.index_source(project_path=PROJECT, source_id=source_id)

    retriever = FakeRetriever()
    model = FakeAnswerModel()
    service = LiteratureService(
        providers=[StaticProvider("alpha", [make_record("alpha", "2401.00001")])],
        project_store=store,
        index_store=index_store,
        retriever=retriever if with_retriever else None,
        answer_model=model if with_answer_model else None,
        now_factory=lambda: NOW,
    )
    return service, store, index_store, source_id, pdf, retriever, model


def page_hits(index_store, source_id: str, pages: Sequence[int]) -> list[RetrievedChunk]:
    hits: list[RetrievedChunk] = []
    for page in pages:
        for chunk_id in index_store.chunk_ids_on_page(page):
            hits.append(RetrievedChunk(chunk_id=chunk_id, source_id=source_id))
    return hits


async def answer(service: LiteratureService, source_id: str, **overrides):
    kwargs = {
        "project_path": PROJECT,
        "question": QUESTION,
        "source_ids": [source_id],
        "top_k": 5,
    }
    kwargs.update(overrides)
    return await service.answer_question(**kwargs)


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestAnswerScope:
    async def test_empty_source_scope_is_refused(self, tmp_path: Path) -> None:
        service, _, _, _, _, retriever, model = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, "", source_ids=[])
        assert excinfo.value.code is LiteratureServiceErrorCode.SCOPE_REQUIRED
        assert retriever.calls == []
        assert model.prompts == []

    async def test_blank_question_is_refused(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, model = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, question="   ")
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert retriever.calls == []
        assert model.prompts == []

    async def test_unknown_source_is_refused_before_retrieval(self, tmp_path: Path) -> None:
        service, _, _, _, _, retriever, model = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, "src_missing000000000000")
        assert excinfo.value.code is LiteratureServiceErrorCode.SOURCE_NOT_FOUND
        assert retriever.calls == []
        assert model.prompts == []

    async def test_duplicate_source_ids_are_collapsed(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, _ = await make_answerable(tmp_path)
        await answer(service, source_id, source_ids=[source_id, source_id])
        assert retriever.calls[0]["source_ids"] == [source_id]

    async def test_top_k_out_of_range_is_refused(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, _ = await make_answerable(tmp_path)
        for top_k in (0, 21):
            with pytest.raises(LiteratureServiceError) as excinfo:
                await answer(service, source_id, top_k=top_k)
            assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert retriever.calls == []

    async def test_retrieval_receives_the_project_and_source_scope(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, _ = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        await answer(service, source_id, top_k=3)
        assert retriever.calls == [
            {
                "project_root": PROJECT,
                "source_ids": [source_id],
                "query": QUESTION,
                "top_k": 3,
            }
        ]


# ---------------------------------------------------------------------------
# No evidence means no model call
# ---------------------------------------------------------------------------


class TestInsufficientEvidence:
    async def test_no_retrieval_hits_skips_the_model(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, _, model = await make_answerable(tmp_path)
        result = await answer(service, source_id)
        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.NO_RETRIEVAL_HITS
        assert result.claims == []
        assert result.evidence == []
        assert result.retrieved_chunk_count == 0
        assert model.prompts == []

    async def test_flat_chunk_without_page_metadata_skips_the_model(self, tmp_path: Path) -> None:
        service, store, index_store, source_id, _, retriever, model = await make_answerable(
            tmp_path
        )
        flat_chunk_id = "flat-translation-chunk-1"
        index_store.chunks[flat_chunk_id] = {
            "chunk_id": flat_chunk_id,
            "doc_id": "doc_flat",
            "text": "translated text without page coordinates",
            "metadata": {"source_id": source_id, "project_root": PROJECT},
        }
        retriever.hits = [RetrievedChunk(chunk_id=flat_chunk_id, source_id=source_id)]

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.NO_RESOLVABLE_EVIDENCE
        assert result.unresolved[0].reason == "missing_page_metadata"
        assert result.unresolved[0].chunk_id == flat_chunk_id
        assert model.prompts == []
        assert store.read_sources(PROJECT)

    async def test_missing_chunk_is_recorded_as_unresolved(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = [RetrievedChunk(chunk_id="chunk_that_vanished", source_id=source_id)]

        result = await answer(service, source_id)

        assert result.insufficient_reason is InsufficientReason.NO_RESOLVABLE_EVIDENCE
        assert result.unresolved[0].reason == "chunk_not_found"
        assert model.prompts == []

    async def test_hit_without_a_source_identity_is_unresolved(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = [RetrievedChunk(chunk_id="chunk_flat_without_source")]

        result = await answer(service, source_id)

        assert result.insufficient_reason is InsufficientReason.NO_RESOLVABLE_EVIDENCE
        assert result.unresolved[0].reason == "missing_source_scope"
        assert model.prompts == []

    async def test_unresolvable_hit_does_not_block_resolvable_ones(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = [
            RetrievedChunk(chunk_id="chunk_that_vanished", source_id=source_id),
            *page_hits(index_store, source_id, [1]),
        ]

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.ANSWERED
        assert [item.reason for item in result.unresolved] == ["chunk_not_found"]
        assert model.prompts


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestAnsweredResult:
    async def test_claim_is_bound_to_resolved_page_evidence(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, pdf, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.ANSWERED
        assert result.insufficient_reason is None
        assert len(result.claims) == 1
        claim = result.claims[0]
        assert len(claim.evidence_ids) == 1
        assert claim.model_provider == model.identity.provider
        assert claim.model_name == model.identity.model
        assert claim.model_config_hash == model.identity.config_hash
        assert claim.generated_at == NOW

        assert len(result.evidence) == 1
        evidence = result.evidence[0]
        assert evidence.span.evidence_id == claim.evidence_ids[0]
        assert evidence.source_id == source_id
        assert evidence.span.page_start == 1
        assert evidence.span.chunk_id == retriever.hits[0].chunk_id
        assert pdf.exists()

    async def test_prompt_only_contains_this_retrievals_evidence(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        result = await answer(service, source_id)

        prompt = model.prompts[0]
        assert prompt.count(result.evidence[0].span.evidence_id) == 1
        for chunk_id in index_store.chunk_ids_on_page(2):
            assert chunk_id not in prompt

    async def test_answer_records_the_configured_model_identity(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        result = await answer(service, source_id)

        assert result.model_provider == model.identity.provider
        assert result.model_name == model.identity.model
        assert result.model_config_hash == model.identity.config_hash
        assert result.question == QUESTION
        assert result.project_root == PROJECT
        assert result.source_ids == [source_id]
        assert result.retrieved_chunk_count == len(retriever.hits)
        assert result.generated_at == NOW

    async def test_every_evidence_source_is_inside_the_requested_scope(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, _ = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1, 2])

        result = await answer(service, source_id)

        assert result.evidence
        assert {item.source_id for item in result.evidence} <= set(result.source_ids)
        assert {item.source_id for item in result.evidence} == {source_id}

    async def test_duplicate_hits_resolve_to_one_candidate(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        hits = page_hits(index_store, source_id, [1])
        retriever.hits = [*hits, *hits]

        result = await answer(service, source_id)

        assert result.retrieved_chunk_count == 1
        assert len(result.evidence) == 1
        assert model.prompts[0].count(result.evidence[0].span.evidence_id) == 1

    async def test_identical_requests_are_not_cached(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        first = await answer(service, source_id)
        second = await answer(service, source_id)

        assert len(model.prompts) == 2
        assert first.claims[0].claim_id == second.claims[0].claim_id

    async def test_multi_page_evidence_keeps_each_page_separate(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, pdf, retriever, model = await make_answerable(tmp_path)
        hits = page_hits(index_store, source_id, [1, 2])
        retriever.hits = hits

        def cite_everything(prompt: str) -> str:
            return answer_claim(EVIDENCE_ID_RE.findall(prompt))

        model.response_factory = cite_everything

        result = await answer(service, source_id)

        pages = {item.span.page_start for item in result.evidence}
        assert pages == {1, 2}
        expected_pages = parsed_pages(pdf)
        for item in result.evidence:
            page_text = expected_pages[item.span.page_start]
            assert page_text[item.span.char_start : item.span.char_end] == item.span.exact_quote


# ---------------------------------------------------------------------------
# Validation failures and model failures stay visible
# ---------------------------------------------------------------------------


class TestAnswerFailureReporting:
    async def test_unknown_evidence_id_is_rejected_but_the_answer_survives(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = json.dumps(
            {
                "claims": [
                    {
                        "text": "A grounded claim.",
                        "evidence_ids": ["evidence_ffffffffffffffffffffffff"],
                        "evidence_status": "supported",
                    }
                ]
            }
        )

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.ALL_CLAIMS_REJECTED
        assert result.rejected_claims[0].reason is RejectedClaimReason.UNKNOWN_EVIDENCE_ID

    async def test_fabricated_page_reference_is_rejected(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        def fabricate(prompt: str) -> str:
            return answer_claim(EVIDENCE_ID_RE.findall(prompt), text="As shown on page 9.")

        model.response_factory = fabricate

        result = await answer(service, source_id)

        assert result.claims == []
        assert result.rejected_claims[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE
        assert result.insufficient_reason is InsufficientReason.ALL_CLAIMS_REJECTED

    async def test_rejected_claim_never_becomes_an_uncited_claim(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = json.dumps(
            {
                "claims": [
                    {
                        "text": "Weak claim without evidence.",
                        "evidence_ids": [],
                        "evidence_status": "supported",
                    }
                ]
            }
        )

        result = await answer(service, source_id)

        assert result.claims == []
        assert result.rejected_claims[0].reason is RejectedClaimReason.MISSING_EVIDENCE

    async def test_model_reported_insufficient_maps_to_its_own_reason(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = json.dumps(
            {
                "claims": [
                    {
                        "text": "The corpus does not answer this question.",
                        "evidence_ids": [],
                        "evidence_status": "insufficient",
                    }
                ]
            }
        )

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.MODEL_REPORTED_INSUFFICIENT
        assert result.rejected_claims[0].reason is RejectedClaimReason.MODEL_REPORTED_INSUFFICIENT

    async def test_empty_claim_list_is_treated_as_model_insufficiency(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = '{"claims":[]}'

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.MODEL_REPORTED_INSUFFICIENT

    async def test_invalid_model_response_is_reported_as_such(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = "I could not find anything useful."

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_INVALID_RESPONSE

    async def test_model_failure_is_not_swallowed(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.failure = RuntimeError("provider exploded")

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_GENERATION_FAILED
        assert excinfo.value.details["exception_type"] == "RuntimeError"

    async def test_missing_answer_model_is_unavailable(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, _ = await make_answerable(
            tmp_path, with_answer_model=False
        )
        retriever.hits = page_hits(index_store, source_id, [1])

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_MODEL_UNAVAILABLE
        assert retriever.calls == []

    async def test_missing_retriever_is_unavailable(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, _, model = await make_answerable(
            tmp_path, with_retriever=False
        )

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)
        assert excinfo.value.code is LiteratureServiceErrorCode.RETRIEVAL_UNAVAILABLE
        assert model.prompts == []

    async def test_retriever_failure_propagates_as_a_controlled_error(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, _ = await make_answerable(tmp_path)
        retriever.failure = LiteratureServiceError(
            LiteratureServiceErrorCode.RETRIEVAL_UNAVAILABLE, "向量库不可用"
        )

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)
        assert excinfo.value.code is LiteratureServiceErrorCode.RETRIEVAL_UNAVAILABLE

    async def test_hit_outside_the_requested_scope_cannot_become_evidence(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        scoped_chunk = index_store.chunk_ids_on_page(1)[0]
        index_store.chunks[scoped_chunk]["metadata"]["source_id"] = OTHER_SOURCE
        retriever.hits = [RetrievedChunk(chunk_id=scoped_chunk, source_id=OTHER_SOURCE)]

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.evidence == []
        assert model.prompts == []


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_retrieved_chunk_rejects_blank_fields() -> None:
    with pytest.raises(ValueError):
        RetrievedChunk(chunk_id="", source_id="src_a")


def test_retrieved_chunk_allows_a_sourceless_hit() -> None:
    """A legacy flat chunk has no source identity; it must stay reportable."""

    chunk = RetrievedChunk(chunk_id="chunk_a")
    assert chunk.source_id == ""


def test_fakes_satisfy_the_service_protocols() -> None:
    from src.literature.service import EvidenceRetriever

    assert isinstance(FakeRetriever(), EvidenceRetriever)
    assert not isinstance(object(), EvidenceRetriever)
