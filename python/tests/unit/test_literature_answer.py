"""P3 unit tests for the evidence-answer core.

The module under test owns the stage gate of
``docs/literature-research-poc-plan.md`` section 5.9: *every rendered claim must
pass machine validation*.  The tests here pin, in order:

* what the model is allowed to see (a closed ``evidence_id`` whitelist);
* what a usable model response looks like, and which malformed responses are
  refused instead of being repaired;
* which claims are rejected and with which reason, including page numbers that
  were never in the cited evidence;
* that a rejected claim is never downgraded into an uncited claim.

Everything is offline and deterministic: no network, no vector store, no model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.literature.answer import (
    ANSWER_PROMPT_VERSION,
    MAX_CLAIM_TEXT_CHARS,
    MAX_CLAIMS,
    MAX_EVIDENCE_IDS_PER_CLAIM,
    MAX_RESPONSE_CHARS,
    AnswerEvidenceCandidate,
    AnswerResponseError,
    AnswerStatus,
    InsufficientReason,
    ParsedAnswerClaim,
    RejectedClaimReason,
    build_answer_prompt,
    build_answer_system_prompt,
    parse_answer_response,
    referenced_page_numbers,
    validate_answer_claims,
)
from src.literature.answer_model import ModelIdentity, build_model_identity
from src.literature.models import EvidenceStatus

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
IDENTITY = build_model_identity(
    provider="openai",
    model="gpt-4o",
    base_url="https://api.openai.com/v1",
)


def candidate(
    evidence_id: str,
    *,
    source_id: str = "src_alpha",
    title: str = "Alpha Paper",
    page: int = 1,
    quote: str = "The reported effect was significant.",
    chunk_id: str | None = None,
) -> AnswerEvidenceCandidate:
    return AnswerEvidenceCandidate(
        evidence_id=evidence_id,
        source_id=source_id,
        title=title,
        page_start=page,
        page_end=page,
        chunk_id=chunk_id or f"chunk_{evidence_id}",
        exact_quote=quote,
        context_before="",
        context_after="",
    )


ALPHA = candidate("evidence_aaaaaaaaaaaaaaaaaaaaaaaa", source_id="src_alpha", page=2)
BETA = candidate(
    "evidence_bbbbbbbbbbbbbbbbbbbbbbbb",
    source_id="src_beta",
    title="Beta Paper",
    page=7,
    quote="A different study reported no effect.",
)
GAMMA = candidate(
    "evidence_cccccccccccccccccccccccc",
    source_id="src_gamma",
    title="Gamma Paper",
    page=3,
    quote="The replication matched the original result.",
)
CANDIDATES = {item.evidence_id: item for item in (ALPHA, BETA, GAMMA)}


def parsed(
    text: str,
    evidence_ids: list[str] | None = None,
    status: EvidenceStatus = EvidenceStatus.SUPPORTED,
) -> ParsedAnswerClaim:
    return ParsedAnswerClaim(
        text=text,
        evidence_ids=list(evidence_ids or []),
        evidence_status=status,
    )


def validate(claims: list[ParsedAnswerClaim]):
    return validate_answer_claims(
        parsed=claims,
        candidates=CANDIDATES,
        identity=IDENTITY,
        generated_at=NOW,
    )


# ---------------------------------------------------------------------------
# Prompt construction: the whitelist is the only way an id can be cited
# ---------------------------------------------------------------------------


class TestAnswerPrompt:
    def test_prompt_lists_every_candidate_evidence_id_exactly_once(self) -> None:
        prompt = build_answer_prompt(
            question="Does the effect hold?", candidates=list(CANDIDATES.values())
        )
        for evidence_id in CANDIDATES:
            assert prompt.count(evidence_id) == 1

    def test_prompt_carries_question_page_title_and_quote(self) -> None:
        prompt = build_answer_prompt(question="Which page reports it?", candidates=[ALPHA])
        assert "Which page reports it?" in prompt
        assert ALPHA.title in prompt
        assert ALPHA.exact_quote in prompt
        assert "2" in prompt

    def test_prompt_is_deterministic_for_the_same_input(self) -> None:
        first = build_answer_prompt(question="q", candidates=[ALPHA, BETA])
        second = build_answer_prompt(question="q", candidates=[ALPHA, BETA])
        assert first == second

    def test_prompt_never_contains_an_id_outside_the_candidates(self) -> None:
        prompt = build_answer_prompt(question="q", candidates=[ALPHA])
        assert BETA.evidence_id not in prompt

    def test_empty_candidate_set_is_refused(self) -> None:
        with pytest.raises(ValueError):
            build_answer_prompt(question="q", candidates=[])

    def test_system_prompt_freezes_the_prompt_version(self) -> None:
        system = build_answer_system_prompt()
        assert ANSWER_PROMPT_VERSION in system


# ---------------------------------------------------------------------------
# Response parsing: malformed output fails loudly
# ---------------------------------------------------------------------------


class TestParseAnswerResponse:
    def test_valid_object_is_parsed(self) -> None:
        claims = parse_answer_response(
            '{"claims":[{"text":"A claim","evidence_ids":["e1"],"evidence_status":"supported"}]}'
        )
        assert len(claims) == 1
        assert claims[0].text == "A claim"
        assert claims[0].evidence_ids == ["e1"]
        assert claims[0].evidence_status is EvidenceStatus.SUPPORTED

    def test_fenced_json_block_is_accepted(self) -> None:
        claims = parse_answer_response(
            '```json\n{"claims":[{"text":"A claim","evidence_ids":["e1"],'
            '"evidence_status":"supported"}]}\n```'
        )
        assert [claim.text for claim in claims] == ["A claim"]

    def test_missing_evidence_ids_defaults_to_empty(self) -> None:
        claims = parse_answer_response(
            '{"claims":[{"text":"Nothing supports this","evidence_status":"insufficient"}]}'
        )
        assert claims[0].evidence_ids == []

    def test_unknown_extra_keys_are_ignored(self) -> None:
        claims = parse_answer_response(
            '{"notes":"ignore me","claims":[{"text":"A claim","evidence_ids":[],'
            '"evidence_status":"insufficient","confidence":0.9}]}'
        )
        assert len(claims) == 1

    def test_empty_claim_list_is_valid_and_empty(self) -> None:
        assert parse_answer_response('{"claims":[]}') == []

    def test_prose_around_json_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('Here is the answer: {"claims":[]} hope it helps')

    def test_non_json_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response("I could not find any evidence.")

    def test_json_array_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('[{"text":"a","evidence_status":"supported"}]')

    def test_missing_claims_key_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"answer":"text"}')

    def test_claims_not_a_list_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":{"text":"a"}}')

    def test_claim_not_an_object_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":["a claim"]}')

    def test_claim_without_text_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":[{"evidence_ids":[],"evidence_status":"supported"}]}')

    def test_claim_with_non_string_text_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":[{"text":7,"evidence_status":"supported"}]}')

    def test_unknown_evidence_status_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":[{"text":"a","evidence_status":"probably"}]}')

    def test_missing_evidence_status_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response('{"claims":[{"text":"a"}]}')

    def test_evidence_ids_not_a_list_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response(
                '{"claims":[{"text":"a","evidence_ids":"e1","evidence_status":"supported"}]}'
            )

    def test_non_string_evidence_id_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response(
                '{"claims":[{"text":"a","evidence_ids":[3],"evidence_status":"supported"}]}'
            )

    def test_oversized_claim_text_is_refused(self) -> None:
        text = "x" * (MAX_CLAIM_TEXT_CHARS + 1)
        payload = json.dumps({"claims": [{"text": text, "evidence_status": "supported"}]})
        with pytest.raises(AnswerResponseError):
            parse_answer_response(payload)

    def test_too_many_evidence_ids_on_one_claim_is_refused(self) -> None:
        ids = [f"e{index}" for index in range(MAX_EVIDENCE_IDS_PER_CLAIM + 1)]
        payload = json.dumps(
            {"claims": [{"text": "a", "evidence_ids": ids, "evidence_status": "supported"}]}
        )
        with pytest.raises(AnswerResponseError):
            parse_answer_response(payload)

    def test_oversized_response_is_refused(self) -> None:
        with pytest.raises(AnswerResponseError):
            parse_answer_response("x" * (MAX_RESPONSE_CHARS + 1))

    def test_claims_beyond_the_limit_are_still_parsed(self) -> None:
        """The limit is a validation reason, not a parse failure."""

        claims = [
            {"text": f"claim {index}", "evidence_ids": [], "evidence_status": "insufficient"}
            for index in range(MAX_CLAIMS + 5)
        ]
        parsed_claims = parse_answer_response(json.dumps({"claims": claims}))
        assert len(parsed_claims) == MAX_CLAIMS + 5


# ---------------------------------------------------------------------------
# Page references: invented pages are detected from the claim text alone
# ---------------------------------------------------------------------------


class TestReferencedPageNumbers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("as shown on page 12", {12}),
            ("as shown on Page 12", {12}),
            ("see p.4 for details", {4}),
            ("see pp. 4-6 for details", {4, 5, 6}),
            ("见第 3 页", {3}),
            ("见第 3-5 页", {3, 4, 5}),
            ("见第3页", {3}),
            ("no page reference here", set()),
            ("the study ran for 5 years", set()),
        ],
    )
    def test_page_references_are_extracted(self, text: str, expected: set[int]) -> None:
        assert referenced_page_numbers(text) == expected

    def test_absurd_range_is_not_expanded_into_a_huge_set(self) -> None:
        pages = referenced_page_numbers("see pp. 1-100000")
        assert pages
        assert not pages & {1, 2, 3, 4}
        assert len(pages) <= 10


# ---------------------------------------------------------------------------
# Claim validation: the stage gate
# ---------------------------------------------------------------------------


class TestValidateAnswerClaims:
    def test_supported_claim_with_known_evidence_is_accepted(self) -> None:
        claims, rejected = validate([parsed("The effect was significant.", [ALPHA.evidence_id])])
        assert rejected == []
        assert len(claims) == 1
        assert claims[0].evidence_ids == [ALPHA.evidence_id]
        assert claims[0].evidence_status is EvidenceStatus.SUPPORTED

    def test_conflicting_claim_with_known_evidence_is_accepted(self) -> None:
        claims, rejected = validate(
            [
                parsed(
                    "The two papers disagree.",
                    [ALPHA.evidence_id, BETA.evidence_id],
                    EvidenceStatus.CONFLICTING,
                )
            ]
        )
        assert rejected == []
        assert claims[0].evidence_status is EvidenceStatus.CONFLICTING

    def test_claim_identity_and_model_record_come_from_the_injected_identity(self) -> None:
        claims, _ = validate([parsed("A claim.", [ALPHA.evidence_id])])
        assert claims[0].model_provider == IDENTITY.provider
        assert claims[0].model_name == IDENTITY.model
        assert claims[0].model_config_hash == IDENTITY.config_hash
        assert claims[0].generated_at == NOW
        assert claims[0].claim_id.startswith("claim_")

    def test_unknown_evidence_id_rejects_the_claim(self) -> None:
        claims, rejected = validate([parsed("Invented.", ["evidence_ffffffffffffffffffffffff"])])
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.UNKNOWN_EVIDENCE_ID
        assert "evidence_ffffffffffffffffffffffff" in rejected[0].detail

    def test_one_unknown_id_rejects_the_whole_claim(self) -> None:
        claims, rejected = validate(
            [parsed("Mixed.", [ALPHA.evidence_id, "evidence_ffffffffffffffffffffffff"])]
        )
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.UNKNOWN_EVIDENCE_ID

    def test_supported_claim_without_evidence_is_rejected(self) -> None:
        claims, rejected = validate([parsed("Bare assertion.")])
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.MISSING_EVIDENCE

    def test_model_reported_insufficient_is_recorded_not_rendered(self) -> None:
        claims, rejected = validate(
            [parsed("The corpus does not answer this.", status=EvidenceStatus.INSUFFICIENT)]
        )
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.MODEL_REPORTED_INSUFFICIENT

    def test_blank_claim_text_is_rejected(self) -> None:
        claims, rejected = validate([parsed("   ", [ALPHA.evidence_id])])
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.EMPTY_CLAIM_TEXT

    def test_page_number_matching_the_evidence_is_accepted(self) -> None:
        claims, rejected = validate([parsed("As shown on page 2.", [ALPHA.evidence_id])])
        assert rejected == []
        assert len(claims) == 1

    def test_page_number_absent_from_the_evidence_is_rejected(self) -> None:
        claims, rejected = validate([parsed("As shown on page 9.", [ALPHA.evidence_id])])
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE
        assert "9" in rejected[0].detail

    def test_chinese_page_reference_is_checked(self) -> None:
        assert validate([parsed("见第 2 页。", [ALPHA.evidence_id])])[1] == []
        rejected = validate([parsed("见第 4 页。", [ALPHA.evidence_id])])[1]
        assert rejected[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE

    def test_page_range_requires_every_page_in_the_evidence(self) -> None:
        claims, _ = validate([parsed("见第 2-2 页。", [ALPHA.evidence_id])])
        assert len(claims) == 1
        rejected = validate([parsed("见第 2-3 页。", [ALPHA.evidence_id])])[1]
        assert rejected[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE

    def test_range_covering_pages_without_evidence_is_rejected(self) -> None:
        claims, rejected = validate(
            [parsed("第 2-7 页均有报告。", [ALPHA.evidence_id, BETA.evidence_id])]
        )
        assert claims == []
        assert rejected[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE

    def test_range_fully_covered_by_two_evidence_pages_is_accepted(self) -> None:
        claims, rejected = validate(
            [parsed("第 2-3 页均有报告。", [ALPHA.evidence_id, GAMMA.evidence_id])]
        )
        assert rejected == []
        assert len(claims) == 1

    def test_page_reference_check_uses_only_the_claim_own_evidence(self) -> None:
        rejected = validate([parsed("As shown on page 7.", [ALPHA.evidence_id])])[1]
        assert rejected[0].reason is RejectedClaimReason.FABRICATED_PAGE_REFERENCE

    def test_duplicate_evidence_ids_are_collapsed(self) -> None:
        claims, rejected = validate([parsed("A claim.", [ALPHA.evidence_id, ALPHA.evidence_id])])
        assert rejected == []
        assert claims[0].evidence_ids == [ALPHA.evidence_id]

    def test_duplicate_claims_collapse_to_one(self) -> None:
        claims, _ = validate(
            [
                parsed("Repeated claim.", [ALPHA.evidence_id]),
                parsed("Repeated claim.", [ALPHA.evidence_id]),
            ]
        )
        assert len(claims) == 1

    def test_claims_beyond_the_limit_are_rejected_with_their_own_reason(self) -> None:
        items = [
            parsed(f"Claim number {index}.", [ALPHA.evidence_id]) for index in range(MAX_CLAIMS + 3)
        ]
        claims, rejected = validate(items)
        assert len(claims) == MAX_CLAIMS
        assert len(rejected) == 3
        assert {item.reason for item in rejected} == {RejectedClaimReason.CLAIM_LIMIT_EXCEEDED}

    def test_rejected_claim_keeps_its_evidence_ids_for_audit(self) -> None:
        rejected = validate([parsed("As shown on page 9.", [ALPHA.evidence_id])])[1]
        assert rejected[0].evidence_ids == [ALPHA.evidence_id]
        assert rejected[0].text == "As shown on page 9."

    def test_no_parsed_claims_yields_no_claims_and_no_rejections(self) -> None:
        claims, rejected = validate([])
        assert claims == []
        assert rejected == []


# ---------------------------------------------------------------------------
# Vocabulary used by the service and the API
# ---------------------------------------------------------------------------


class TestAnswerVocabulary:
    def test_status_values_are_frozen(self) -> None:
        assert {status.value for status in AnswerStatus} == {"answered", "insufficient"}

    def test_insufficient_reasons_are_frozen(self) -> None:
        assert {reason.value for reason in InsufficientReason} == {
            "no_retrieval_hits",
            "no_resolvable_evidence",
            "model_reported_insufficient",
            "all_claims_rejected",
        }

    def test_rejected_claim_reasons_are_frozen(self) -> None:
        assert {reason.value for reason in RejectedClaimReason} == {
            "unknown_evidence_id",
            "fabricated_page_reference",
            "missing_evidence",
            "model_reported_insufficient",
            "empty_claim_text",
            "claim_limit_exceeded",
        }

    def test_model_identity_rejects_a_blank_provider(self) -> None:
        with pytest.raises(ValueError):
            ModelIdentity(provider="", model="gpt-4o", config_hash="a" * 64)
