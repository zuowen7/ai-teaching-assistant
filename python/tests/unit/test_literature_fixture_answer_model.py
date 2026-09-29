"""Unit tests for the deterministic, non-LLM fixture answer model (D-040)."""

from __future__ import annotations

import json

import pytest

from src.literature.answer import AnswerEvidenceCandidate, build_answer_prompt
from src.literature.fixture_answer_model import (
    FIXTURE_MODEL_NAME,
    FIXTURE_PROVIDER_NAME,
    FixtureAnswerModel,
    first_sentence,
    parse_prompt_evidence,
)

EVIDENCE_A = "evidence_" + "a" * 24
EVIDENCE_B = "evidence_" + "b" * 24


def candidate(evidence_id: str, quote: str, page: int = 1) -> AnswerEvidenceCandidate:
    return AnswerEvidenceCandidate(
        evidence_id=evidence_id,
        source_id="src_demo_0001",
        title="Demo Paper A",
        page_start=page,
        page_end=page,
        chunk_id=f"chunk_{evidence_id[-4:]}",
        exact_quote=quote,
        context_before="before",
        context_after="after",
    )


def prompt_for(*candidates: AnswerEvidenceCandidate) -> str:
    return build_answer_prompt(question="Which protocol is used?", candidates=list(candidates))


class TestIdentity:
    def test_identity_marks_the_answer_as_a_fixture(self) -> None:
        identity = FixtureAnswerModel().identity

        assert identity.provider == FIXTURE_PROVIDER_NAME
        assert identity.model == FIXTURE_MODEL_NAME
        assert len(identity.config_hash) == 64

    def test_identity_hash_is_stable_and_secret_free(self) -> None:
        first = FixtureAnswerModel().identity
        second = FixtureAnswerModel().identity

        assert first.config_hash == second.config_hash
        assert "sk-" not in json.dumps(first.model_dump())


class TestPromptParsing:
    def test_multi_line_quote_is_recovered_whole(self) -> None:
        quote = "First line of the page text.\nSecond line continues here."
        prompt = prompt_for(candidate(EVIDENCE_A, quote))

        parsed = parse_prompt_evidence(prompt)

        assert [entry[0] for entry in parsed] == [EVIDENCE_A]
        assert parsed[0][1] == quote

    def test_context_lines_are_not_part_of_the_quote(self) -> None:
        prompt = prompt_for(candidate(EVIDENCE_A, "Only this sentence."))

        quote = parse_prompt_evidence(prompt)[0][1]

        assert quote == "Only this sentence."
        assert "before" not in quote
        assert "after" not in quote

    def test_two_candidates_are_kept_in_prompt_order(self) -> None:
        prompt = prompt_for(
            candidate(EVIDENCE_A, "Alpha sentence."),
            candidate(EVIDENCE_B, "Beta sentence.", page=2),
        )

        assert [entry[0] for entry in parse_prompt_evidence(prompt)] == [EVIDENCE_A, EVIDENCE_B]

    def test_the_reply_template_is_not_part_of_the_last_quote(self) -> None:
        prompt = prompt_for(candidate(EVIDENCE_A, "Only this sentence."))

        quote = parse_prompt_evidence(prompt)[0][1]

        assert "Reply with a single JSON object" not in quote
        assert "claims" not in quote


class TestFirstSentence:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("One sentence. And another.", "One sentence."),
            ("Line one\nline two ends here.", "Line one line two ends here."),
            ("第一句。第二句。", "第一句。"),
            ("no terminator at all", "no terminator at all"),
        ],
    )
    def test_sentence_extraction(self, text: str, expected: str) -> None:
        assert first_sentence(text) == expected

    def test_long_excerpt_is_truncated_with_a_marker(self) -> None:
        excerpt = first_sentence("word " * 200)

        assert len(excerpt) <= 241
        assert excerpt.endswith("…")


class TestComplete:
    async def test_claims_quote_their_own_evidence(self) -> None:
        model = FixtureAnswerModel()
        prompt = prompt_for(
            candidate(EVIDENCE_A, "The study uses a held-out split. More text."),
            candidate(EVIDENCE_B, "A second finding is reported.", page=2),
        )

        payload = json.loads(await model.complete(system_prompt="s", prompt=prompt))

        assert [claim["evidence_ids"] for claim in payload["claims"]] == [
            [EVIDENCE_A],
            [EVIDENCE_B],
        ]
        assert payload["claims"][0]["text"] == "The study uses a held-out split."
        assert all(claim["evidence_status"] == "supported" for claim in payload["claims"])

    async def test_repeated_calls_are_identical(self) -> None:
        model = FixtureAnswerModel()
        prompt = prompt_for(candidate(EVIDENCE_A, "Same input, same output."))

        first = await model.complete(system_prompt="s", prompt=prompt)
        second = await model.complete(system_prompt="s", prompt=prompt)

        assert first == second

    async def test_claim_count_respects_the_cap(self) -> None:
        model = FixtureAnswerModel(max_claims=1)
        prompt = prompt_for(
            candidate(EVIDENCE_A, "Alpha sentence."),
            candidate(EVIDENCE_B, "Beta sentence."),
        )

        payload = json.loads(await model.complete(system_prompt="s", prompt=prompt))

        assert len(payload["claims"]) == 1

    async def test_a_prompt_without_evidence_yields_no_claims(self) -> None:
        model = FixtureAnswerModel()

        payload = json.loads(await model.complete(system_prompt="s", prompt="no evidence here"))

        assert payload == {"claims": []}

    async def test_an_empty_quote_is_skipped(self) -> None:
        model = FixtureAnswerModel()
        prompt = prompt_for(candidate(EVIDENCE_A, "   "), candidate(EVIDENCE_B, "Real text."))

        payload = json.loads(await model.complete(system_prompt="s", prompt=prompt))

        assert [claim["evidence_ids"] for claim in payload["claims"]] == [[EVIDENCE_B]]
