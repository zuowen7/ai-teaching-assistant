"""Deterministic, non-LLM answer model for offline demos and regression tests.

The PoC's real answer path calls a model.  When no model is available (offline
demo, defense laptop, CI), the chain still has to be demonstrable — but it must
never look like a model produced the answer.

This model does **not** read the research question and does not summarise.  It
extracts one verbatim sentence per retrieved evidence candidate, so every claim it
returns is literally present in the citation it carries.  Its identity records
``provider="fixture"`` and ``model="deterministic-extractive-v1"``, which flows
into ``model_provider``/``model_name``/``model_config_hash`` on every answer, so a
fixture answer stays distinguishable from a model answer.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.literature.answer import MAX_CLAIMS, AnswerEvidenceCandidate
from src.literature.answer_model import ModelIdentity, build_model_identity

FIXTURE_PROVIDER_NAME = "fixture"
FIXTURE_MODEL_NAME = "deterministic-extractive-v1"
DEFAULT_MAX_CLAIMS = 3
MAX_EXCERPT_CHARS = 240

#: Matches one prompt entry header produced by ``answer._prompt_entry``.
_ENTRY_HEADER_RE = re.compile(
    r"^\[(?P<evidence_id>[A-Za-z0-9_]+)\] title: (?P<title>.*) \| "
    r"page: (?P<page>\d+) \| source: (?P<source>.*)$"
)
_CONTEXT_PREFIXES = ("context before:", "context after:")
#: CJK terminators stand alone; Latin ones must end a word ("3.5" is not a stop).
_SENTENCE_END_RE = re.compile(r"[。！？]|[.!?](?=\s|$)")
_EVIDENCE_HEADER = "Evidence:\n"
_EVIDENCE_TAIL_MARKERS = ("\n\nReply with", "\n\nAnswer the research question")


def first_sentence(text: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Return the opening sentence of ``text`` as a single normalised line."""

    flat = " ".join(text.split())
    match = _SENTENCE_END_RE.search(flat)
    excerpt = flat[: match.end()].strip() if match else flat
    if len(excerpt) > limit:
        excerpt = excerpt[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return excerpt


def _evidence_section(prompt: str) -> str:
    """Return only the evidence list, without the question or the reply template."""

    section = prompt
    if _EVIDENCE_HEADER in section:
        section = section.split(_EVIDENCE_HEADER, 1)[1]
    for marker in _EVIDENCE_TAIL_MARKERS:
        if marker in section:
            section = section.split(marker, 1)[0]
    return section


def parse_prompt_evidence(prompt: str) -> list[tuple[str, str]]:
    """Recover ``(evidence_id, quote)`` pairs from an answer prompt.

    The prompt format is ours and is versioned by ``ANSWER_PROMPT_VERSION``; this
    parser is tolerant of quotes that span several lines.
    """

    entries: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None

    for line in _evidence_section(prompt).split("\n"):
        header = _ENTRY_HEADER_RE.match(line)
        if header is not None:
            if current is not None:
                entries.append(current)
            current = (header.group("evidence_id"), [])
            continue
        if current is None:
            continue
        if line.startswith(_CONTEXT_PREFIXES):
            continue
        current[1].append(line)
    if current is not None:
        entries.append(current)

    parsed: list[tuple[str, str]] = []
    for evidence_id, body in entries:
        quote = "\n".join(body).strip()
        if quote.startswith("quote:"):
            quote = quote[len("quote:") :]
        parsed.append((evidence_id, quote.strip()))
    return parsed


class FixtureAnswerModel:
    """Extractive answer model with no network and no sampling."""

    def __init__(self, *, max_claims: int = DEFAULT_MAX_CLAIMS) -> None:
        self._max_claims = max(0, min(int(max_claims), MAX_CLAIMS))
        self._identity = build_model_identity(
            provider=FIXTURE_PROVIDER_NAME,
            model=FIXTURE_MODEL_NAME,
        )

    @property
    def identity(self) -> ModelIdentity:
        return self._identity

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        claims: list[dict[str, Any]] = []
        for evidence_id, quote in parse_prompt_evidence(prompt):
            excerpt = first_sentence(quote)
            if not excerpt:
                continue
            claims.append(
                {
                    "text": excerpt,
                    "evidence_ids": [evidence_id],
                    "evidence_status": "supported",
                }
            )
            if len(claims) >= self._max_claims:
                break
        return json.dumps({"claims": claims}, ensure_ascii=False)


def candidates_from_prompt(prompt: str) -> list[AnswerEvidenceCandidate]:
    """Test helper: turn a prompt back into the candidates it was built from."""

    return [
        AnswerEvidenceCandidate(
            evidence_id=evidence_id,
            source_id="",
            title="",
            page_start=1,
            page_end=1,
            chunk_id="",
            exact_quote=quote,
        )
        for evidence_id, quote in parse_prompt_evidence(prompt)
    ]
