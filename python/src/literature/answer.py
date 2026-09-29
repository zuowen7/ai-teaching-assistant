"""Pure logic for evidence-grounded answers (plan 5.9).

This module owns the P3 stage gate: *every rendered claim must pass machine
validation*.  It performs no I/O and knows nothing about retrieval, providers or
the project store, which makes each rejection reason individually testable.

The rules implemented here are frozen in ``docs/literature-research-poc-plan.md``
section 5.9:

* the model may only cite ``evidence_id`` values the server put in the prompt;
* a page number inside a claim must belong to that claim's own evidence;
* a claim that fails validation is *rejected with a reason*, never downgraded
  into an uncited claim;
* a malformed model response fails explicitly instead of being repaired.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from src.literature.models import AnswerClaim, EvidenceStatus

ANSWER_PROMPT_VERSION = "evidence_answer_v1"

MAX_CLAIMS = 20
MAX_CLAIM_TEXT_CHARS = 20_000
MAX_EVIDENCE_IDS_PER_CLAIM = 100
MAX_RESPONSE_CHARS = 200_000
MAX_PROMPT_CHARS = 60_000
MAX_PROMPT_QUOTE_CHARS = 1_200
MAX_PROMPT_CONTEXT_CHARS = 300
MAX_PAGE_RANGE = 500

_UNVERIFIABLE_PAGE = -1
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT = "insufficient"


class InsufficientReason(StrEnum):
    NO_RETRIEVAL_HITS = "no_retrieval_hits"
    NO_RESOLVABLE_EVIDENCE = "no_resolvable_evidence"
    MODEL_REPORTED_INSUFFICIENT = "model_reported_insufficient"
    ALL_CLAIMS_REJECTED = "all_claims_rejected"


class RejectedClaimReason(StrEnum):
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    FABRICATED_PAGE_REFERENCE = "fabricated_page_reference"
    MISSING_EVIDENCE = "missing_evidence"
    MODEL_REPORTED_INSUFFICIENT = "model_reported_insufficient"
    EMPTY_CLAIM_TEXT = "empty_claim_text"
    CLAIM_LIMIT_EXCEEDED = "claim_limit_exceeded"


class AnswerResponseError(ValueError):
    """The model response did not satisfy the frozen output contract."""


@runtime_checkable
class ClaimModelIdentity(Protocol):
    """Structural view of the model record every claim must carry."""

    provider: str
    model: str
    config_hash: str


class AnswerEvidenceCandidate(BaseModel):
    """One resolved, machine-verified evidence span offered to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=500)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, max_length=128)
    exact_quote: str = Field(min_length=1, max_length=100_000)
    context_before: str = Field(default="", max_length=100_000)
    context_after: str = Field(default="", max_length=100_000)


class ParsedAnswerClaim(BaseModel):
    """One claim as the model returned it, before any validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus


class RejectedClaim(BaseModel):
    """A claim that failed machine validation, kept for the user to inspect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="", max_length=MAX_CLAIM_TEXT_CHARS)
    reason: RejectedClaimReason
    detail: str = Field(default="", max_length=2_000)
    evidence_ids: list[str] = Field(default_factory=list)


class UnresolvedEvidence(BaseModel):
    """A retrieval hit that could not be turned into verifiable evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(default="", max_length=64)
    chunk_id: str | None = Field(default=None, max_length=128)
    reason: str = Field(min_length=1, max_length=64)
    detail: str = Field(default="", max_length=1_000)


_PAGE_RANGE_PATTERNS = (
    re.compile(r"第\s*(\d{1,5})\s*[-–—~至到]\s*(\d{1,5})\s*页"),
    re.compile(r"(?i)\bpp?\.\s*(\d{1,5})\s*[-–—~]\s*(\d{1,5})"),
    re.compile(r"(?i)\bpages?\s+(\d{1,5})\s*[-–—~]\s*(\d{1,5})"),
)
_PAGE_SINGLE_PATTERNS = (
    re.compile(r"第\s*(\d{1,5})\s*页"),
    re.compile(r"(?i)\bpp?\.\s*(\d{1,5})\b"),
    re.compile(r"(?i)\bpages?\s+(\d{1,5})\b"),
)


def _collapse_whitespace(value: str) -> str:
    """Collapse whitespace and drop control characters a model may emit."""

    return _WHITESPACE_RE.sub(" ", _CONTROL_RE.sub("", value)).strip()


def referenced_page_numbers(text: str) -> set[int]:
    """Return every PDF page number the claim text explicitly names.

    A range wider than :data:`MAX_PAGE_RANGE` cannot be verified against a small
    evidence set, so it collapses to the sentinel ``-1``, which never matches a
    real page and therefore rejects the claim.
    """

    pages: set[int] = set()
    for pattern in _PAGE_RANGE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                start, end = end, start
            if end - start > MAX_PAGE_RANGE:
                return {_UNVERIFIABLE_PAGE}
            pages.update(range(start, end + 1))
    for pattern in _PAGE_SINGLE_PATTERNS:
        for match in pattern.finditer(text):
            pages.add(int(match.group(1)))
    return pages


def build_answer_system_prompt() -> str:
    """Freeze the answering persona and its prompt version."""

    return (
        "You are an evidence-grounded academic research assistant. "
        "You answer only from the evidence supplied in the request, you never "
        "invent a source, page number or quotation, and you state explicitly "
        "when the evidence is insufficient. "
        f"Prompt version: {ANSWER_PROMPT_VERSION}."
    )


def _prompt_entry(candidate: AnswerEvidenceCandidate) -> str:
    quote = candidate.exact_quote[:MAX_PROMPT_QUOTE_CHARS]
    before = candidate.context_before[:MAX_PROMPT_CONTEXT_CHARS]
    after = candidate.context_after[:MAX_PROMPT_CONTEXT_CHARS]
    lines = [
        f"[{candidate.evidence_id}] title: {candidate.title} | "
        f"page: {candidate.page_start} | source: {candidate.source_id}",
        f"quote: {quote}",
    ]
    if before:
        lines.append(f"context before: {before}")
    if after:
        lines.append(f"context after: {after}")
    return "\n".join(lines)


def build_answer_prompt(
    *,
    question: str,
    candidates: Sequence[AnswerEvidenceCandidate],
) -> str:
    """Build the user prompt; the candidate list is the citation whitelist."""

    if not candidates:
        raise ValueError("at least one evidence candidate is required")

    entries = "\n\n".join(_prompt_entry(candidate) for candidate in candidates)
    prompt = (
        "Answer the research question using only the evidence listed below.\n"
        "Every claim must cite one or more evidence ids copied exactly from the\n"
        "square-bracket ids in this list. Never invent a source id or a page number.\n\n"
        f"Research question:\n{question}\n\n"
        f"Evidence:\n{entries}\n\n"
        "Reply with a single JSON object and nothing else:\n"
        '{"claims":[{"text":"<one sentence>","evidence_ids":["<evidence id>"],'
        '"evidence_status":"supported|conflicting|insufficient"}]}\n'
        'Use "insufficient" with an empty evidence_ids list for anything the '
        "listed evidence does not support."
    )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("answer prompt exceeds the size limit")
    return prompt


def _strip_single_fence(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        raise AnswerResponseError("model response is empty")
    opener = lines[0].strip().lower()
    if opener not in {"```", "```json"}:
        raise AnswerResponseError("unsupported code fence in the model response")
    if len(lines) < 2 or lines[-1].strip() != "```":
        raise AnswerResponseError("unterminated code fence in the model response")
    return "\n".join(lines[1:-1]).strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _strip_single_fence(stripped)
    if not stripped.startswith("{") or not stripped.endswith("}"):
        raise AnswerResponseError("model response must be a single JSON object")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AnswerResponseError("model response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AnswerResponseError("model response must be a single JSON object")
    return payload


def _parse_claim(item: Any, *, index: int) -> ParsedAnswerClaim:
    if not isinstance(item, dict):
        raise AnswerResponseError(f"claim {index} must be a JSON object")

    text = item.get("text")
    if not isinstance(text, str):
        raise AnswerResponseError(f"claim {index} must carry a text string")
    if len(text) > MAX_CLAIM_TEXT_CHARS:
        raise AnswerResponseError(f"claim {index} text exceeds the size limit")

    raw_ids = item.get("evidence_ids") or []
    if not isinstance(raw_ids, list):
        raise AnswerResponseError(f"claim {index} evidence_ids must be a list")
    if len(raw_ids) > MAX_EVIDENCE_IDS_PER_CLAIM:
        raise AnswerResponseError(f"claim {index} cites too many evidence ids")
    evidence_ids: list[str] = []
    for value in raw_ids:
        if not isinstance(value, str):
            raise AnswerResponseError(f"claim {index} evidence ids must be strings")
        evidence_ids.append(value)

    raw_status = item.get("evidence_status")
    if not isinstance(raw_status, str):
        raise AnswerResponseError(f"claim {index} must carry an evidence_status")
    try:
        status = EvidenceStatus(raw_status.strip().lower())
    except ValueError as exc:
        raise AnswerResponseError(f"claim {index} has an unknown evidence_status") from exc

    return ParsedAnswerClaim(text=text, evidence_ids=evidence_ids, evidence_status=status)


def parse_answer_response(text: str) -> list[ParsedAnswerClaim]:
    """Parse the model response, refusing anything outside the frozen contract."""

    if not isinstance(text, str):
        raise AnswerResponseError("model response must be text")
    if len(text) > MAX_RESPONSE_CHARS:
        raise AnswerResponseError("model response exceeds the size limit")

    payload = _extract_json_object(text)
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise AnswerResponseError("model response must contain a claims list")
    return [_parse_claim(item, index=index) for index, item in enumerate(raw_claims)]


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _reject(
    text: str,
    reason: RejectedClaimReason,
    *,
    detail: str = "",
    evidence_ids: Sequence[str] = (),
) -> RejectedClaim:
    return RejectedClaim(
        text=text[:MAX_CLAIM_TEXT_CHARS],
        reason=reason,
        detail=detail,
        evidence_ids=list(evidence_ids)[:MAX_EVIDENCE_IDS_PER_CLAIM],
    )


def validate_answer_claims(
    *,
    parsed: Sequence[ParsedAnswerClaim],
    candidates: Mapping[str, AnswerEvidenceCandidate],
    identity: ClaimModelIdentity,
    generated_at: Any,
) -> tuple[list[AnswerClaim], list[RejectedClaim]]:
    """Turn parsed claims into contract claims, rejecting what fails validation."""

    claims: list[AnswerClaim] = []
    rejected: list[RejectedClaim] = []
    seen_claim_ids: set[str] = set()

    for index, item in enumerate(parsed):
        text = _collapse_whitespace(item.text)
        evidence_ids = _dedupe(item.evidence_ids)

        if index >= MAX_CLAIMS:
            rejected.append(
                _reject(
                    text,
                    RejectedClaimReason.CLAIM_LIMIT_EXCEEDED,
                    detail=f"claim index {index} exceeds the limit of {MAX_CLAIMS}",
                    evidence_ids=evidence_ids,
                )
            )
            continue
        if not text:
            rejected.append(_reject(text, RejectedClaimReason.EMPTY_CLAIM_TEXT))
            continue
        if item.evidence_status is EvidenceStatus.INSUFFICIENT:
            rejected.append(
                _reject(
                    text,
                    RejectedClaimReason.MODEL_REPORTED_INSUFFICIENT,
                    detail="the model reported that the evidence is insufficient",
                    evidence_ids=evidence_ids,
                )
            )
            continue

        unknown = [evidence_id for evidence_id in evidence_ids if evidence_id not in candidates]
        if unknown:
            rejected.append(
                _reject(
                    text,
                    RejectedClaimReason.UNKNOWN_EVIDENCE_ID,
                    detail="evidence ids outside this retrieval: " + ", ".join(unknown),
                    evidence_ids=evidence_ids,
                )
            )
            continue
        if not evidence_ids:
            rejected.append(
                _reject(
                    text,
                    RejectedClaimReason.MISSING_EVIDENCE,
                    detail="a supported or conflicting claim must cite evidence",
                )
            )
            continue

        evidence_pages: set[int] = set()
        for evidence_id in evidence_ids:
            candidate = candidates[evidence_id]
            evidence_pages.add(candidate.page_start)
            evidence_pages.add(candidate.page_end)
        unverified = sorted(
            page for page in referenced_page_numbers(text) if page not in evidence_pages
        )
        if unverified:
            rejected.append(
                _reject(
                    text,
                    RejectedClaimReason.FABRICATED_PAGE_REFERENCE,
                    detail="page references outside the cited evidence: "
                    + ", ".join(str(page) for page in unverified),
                    evidence_ids=evidence_ids,
                )
            )
            continue

        claim = AnswerClaim(
            text=text,
            evidence_ids=evidence_ids,
            evidence_status=item.evidence_status,
            model_provider=identity.provider,
            model_name=identity.model,
            model_config_hash=identity.config_hash,
            generated_at=generated_at,
        )
        if claim.claim_id in seen_claim_ids:
            continue
        seen_claim_ids.add(claim.claim_id)
        claims.append(claim)

    return claims, rejected
