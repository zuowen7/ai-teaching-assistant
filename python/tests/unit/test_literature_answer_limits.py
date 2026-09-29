"""P3 boundary and extreme-case tests for the answer service (plan 5.9).

These tests deliberately push the contract past its normal operating range: a
question that cannot fit the prompt budget, more sources or hits than the request
allows, a model that answers with a flood of claims, control characters, a
concurrent pair of questions against one project, and an artifact that is
replaced between retrieval and evidence resolution.

The point is not that these inputs are realistic; it is that each one either
succeeds honestly or fails with a named reason, and never produces a plausible
looking citation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.literature.answer import (
    MAX_CLAIMS,
    MAX_PROMPT_CHARS,
    AnswerStatus,
    InsufficientReason,
    RejectedClaimReason,
)
from src.literature.service import (
    MAX_ANSWER_SOURCES,
    MAX_ANSWER_TOP_K,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
    RetrievedChunk,
)
from tests.unit.test_literature_answer_service import (
    EVIDENCE_ID_RE,
    answer,
    answer_claim,
    make_answerable,
    page_hits,
)


class MutatingRetriever:
    """Retriever that rewrites the artifact before evidence resolution runs."""

    def __init__(self, hits: list[RetrievedChunk], artifact: Path, replacement: bytes) -> None:
        self.hits = hits
        self.artifact = artifact
        self.replacement = replacement

    async def retrieve(self, *, project_root, source_ids, query, top_k):
        self.artifact.write_bytes(self.replacement)
        return list(self.hits)


class FloodingRetriever:
    """Retriever that ignores top_k and returns far more hits than allowed."""

    def __init__(self, count: int) -> None:
        self.count = count

    async def retrieve(self, *, project_root, source_ids, query, top_k):
        return [
            RetrievedChunk(chunk_id=f"chunk_flood_{index:04d}", source_id="src_alpha")
            for index in range(self.count)
        ]


# ---------------------------------------------------------------------------
# Request-size boundaries
# ---------------------------------------------------------------------------


class TestRequestBoundaries:
    async def test_question_at_the_documented_limit_is_accepted(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, _ = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        result = await answer(service, source_id, question="q" * 2000)

        assert result.status is AnswerStatus.ANSWERED
        assert result.question == "q" * 2000

    async def test_question_beyond_the_prompt_budget_fails_explicitly(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, question="q" * (MAX_PROMPT_CHARS + 10))

        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert excinfo.value.details["reason"]
        assert model.prompts == []

    async def test_question_whitespace_only_is_refused(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, _, _ = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, question=" \t \n ")
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID

    async def test_source_scope_above_the_limit_is_refused(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, retriever, _ = await make_answerable(tmp_path)
        too_many = [source_id] + [f"src_pad{index:013d}" for index in range(MAX_ANSWER_SOURCES)]

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, source_ids=too_many)

        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert excinfo.value.details["source_count"] == MAX_ANSWER_SOURCES + 1
        assert retriever.calls == []

    @pytest.mark.parametrize("top_k", [0, -1, MAX_ANSWER_TOP_K + 1, 10_000])
    async def test_top_k_boundaries_are_refused(self, tmp_path: Path, top_k: int) -> None:
        service, _, _, source_id, _, retriever, _ = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, top_k=top_k)
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert retriever.calls == []

    async def test_boolean_top_k_is_refused(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, _, _ = await make_answerable(tmp_path)
        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id, top_k=True)
        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID

    async def test_retriever_returning_more_than_top_k_fails_loudly(self, tmp_path: Path) -> None:
        service, _, _, source_id, _, _, model = await make_answerable(tmp_path)
        service._retriever = FloodingRetriever(MAX_ANSWER_TOP_K + 1)

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)

        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID
        assert excinfo.value.details["reason"] == "too_many_retrieval_hits"
        assert model.prompts == []

    async def test_duplicate_source_ids_do_not_hide_the_limit(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, _ = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        result = await answer(service, source_id, source_ids=[source_id] * (MAX_ANSWER_SOURCES + 5))

        assert result.source_ids == [source_id]


# ---------------------------------------------------------------------------
# Model output boundaries
# ---------------------------------------------------------------------------


class TestModelOutputBoundaries:
    async def test_a_flood_of_claims_is_capped_and_reported(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        def flood(prompt: str) -> str:
            evidence_id = EVIDENCE_ID_RE.findall(prompt)[0]
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": f"Claim number {index}.",
                            "evidence_ids": [evidence_id],
                            "evidence_status": "supported",
                        }
                        for index in range(MAX_CLAIMS + 7)
                    ]
                }
            )

        model.response_factory = flood

        result = await answer(service, source_id)

        assert len(result.claims) == MAX_CLAIMS
        assert len(result.rejected_claims) == 7
        assert {item.reason for item in result.rejected_claims} == {
            RejectedClaimReason.CLAIM_LIMIT_EXCEEDED
        }

    async def test_oversized_claim_text_invalidates_the_whole_response(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response = json.dumps(
            {
                "claims": [
                    {
                        "text": "x" * 20_001,
                        "evidence_ids": [],
                        "evidence_status": "supported",
                    }
                ]
            }
        )

        with pytest.raises(LiteratureServiceError) as excinfo:
            await answer(service, source_id)

        assert excinfo.value.code is LiteratureServiceErrorCode.ANSWER_INVALID_RESPONSE

    async def test_control_characters_are_stripped_from_claim_text(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        def noisy(prompt: str) -> str:
            evidence_id = EVIDENCE_ID_RE.findall(prompt)[0]
            return answer_claim([evidence_id], text="A\x00 grounded\x07 claim.")

        model.response_factory = noisy

        result = await answer(service, source_id)

        assert result.claims[0].text == "A grounded claim."

    async def test_repeated_evidence_ids_collapse_in_the_claim(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        def repeat(prompt: str) -> str:
            evidence_id = EVIDENCE_ID_RE.findall(prompt)[0]
            return answer_claim([evidence_id, evidence_id, evidence_id])

        model.response_factory = repeat

        result = await answer(service, source_id)

        assert result.claims[0].evidence_ids == [result.evidence[0].span.evidence_id]
        assert len(result.evidence) == 1

    async def test_long_quote_is_truncated_in_prompt_but_evidence_stays_exact(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(
            tmp_path, page_texts=("word " * 600,)
        )
        retriever.hits = page_hits(index_store, source_id, [1])
        model.response_factory = lambda prompt: answer_claim(EVIDENCE_ID_RE.findall(prompt))

        result = await answer(service, source_id, top_k=MAX_ANSWER_TOP_K)

        # The prompt budget may shorten what the model reads, but the stored
        # evidence keeps the full coordinate-derived quote.
        assert result.status is AnswerStatus.ANSWERED
        assert result.evidence
        assert max(len(item.span.exact_quote) for item in result.evidence) > 1_200
        for item in result.evidence:
            assert item.span.exact_quote.strip()
            assert item.span.quote_sha256


# ---------------------------------------------------------------------------
# Concurrency and mutation during an answer
# ---------------------------------------------------------------------------


class TestConcurrencyAndMutation:
    async def test_concurrent_questions_on_one_project_stay_independent(
        self, tmp_path: Path
    ) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        results = await asyncio.gather(
            *(answer(service, source_id, question=f"Question {index}?") for index in range(5))
        )

        assert all(result.status is AnswerStatus.ANSWERED for result in results)
        assert len(model.prompts) == 5
        assert {result.question for result in results} == {
            f"Question {index}?" for index in range(5)
        }
        assert all(len(result.claims) == 1 for result in results)

    async def test_artifact_replaced_mid_answer_yields_no_evidence(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, pdf, _, model = await make_answerable(tmp_path)
        hits = page_hits(index_store, source_id, [1])
        service._retriever = MutatingRetriever(hits, pdf, b"%PDF-1.4 replaced artifact")

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.INSUFFICIENT
        assert result.insufficient_reason is InsufficientReason.NO_RESOLVABLE_EVIDENCE
        assert result.evidence == []
        assert result.unresolved[0].reason == "artifact_hash_mismatch"
        assert model.prompts == []

    async def test_rejected_claims_survive_alongside_accepted_ones(self, tmp_path: Path) -> None:
        service, _, index_store, source_id, _, retriever, model = await make_answerable(tmp_path)
        retriever.hits = page_hits(index_store, source_id, [1])

        def mixed(prompt: str) -> str:
            evidence_id = EVIDENCE_ID_RE.findall(prompt)[0]
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": "A grounded claim.",
                            "evidence_ids": [evidence_id],
                            "evidence_status": "supported",
                        },
                        {
                            "text": "As shown on page 42.",
                            "evidence_ids": [evidence_id],
                            "evidence_status": "supported",
                        },
                        {
                            "text": "The corpus is silent on this.",
                            "evidence_ids": [],
                            "evidence_status": "insufficient",
                        },
                    ]
                }
            )

        model.response_factory = mixed

        result = await answer(service, source_id)

        assert result.status is AnswerStatus.ANSWERED
        assert [claim.text for claim in result.claims] == ["A grounded claim."]
        assert {item.reason for item in result.rejected_claims} == {
            RejectedClaimReason.FABRICATED_PAGE_REFERENCE,
            RejectedClaimReason.MODEL_REPORTED_INSUFFICIENT,
        }
