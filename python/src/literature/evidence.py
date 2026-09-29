"""Page-aware chunking and machine-checkable evidence resolution.

This module owns the P2B invariants of ``docs/literature-research-poc-plan.md``
(sections 4.3, 4.5 and the P2B row of section 5):

* retrieval chunks never cross a PDF page;
* every chunk carries the artifact SHA-256, its page, its character coordinates
  in ``normalized_page_text_v1`` space and the parser/chunker/embedding/index
  versions that produced it;
* an evidence span is *derived* from those coordinates instead of being trusted,
  so a hit can only resolve when the coordinates, the chunk identity and the
  document hash all agree.

The normalization rules below define the ``normalized_page_text_v1`` coordinate
space.  They are frozen once used by an index: changing them invalidates every
existing index (see :func:`index_fingerprint`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.literature.models import EvidenceSpan

# Manual version markers.  Bump PARSER_VERSION when extraction behavior changes,
# CHUNKER_VERSION when the slicing rules change and INDEX_VERSION when the
# stored metadata contract changes; each bump makes existing indexes stale.
PARSER_VERSION = "parser-v1"
CHUNKER_VERSION = "page-chunker-v1"
INDEX_VERSION = "index-v1"

# ChromaDB is used without an explicit embedding function, so the honest
# identity is the library default rather than a pinned model artifact.
DEFAULT_EMBEDDING_MODEL = "chromadb-default"
DEFAULT_EMBEDDING_VERSION = "unpinned"

CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_CHARS = 180
DEFAULT_CONTEXT_CHARS = 200

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 1024 * 1024
_BOUNDARY_PREFERENCES = ("\n\n", "\n", ". ")

EVIDENCE_METADATA_KEYS = (
    "artifact_sha256",
    "page_start",
    "char_start",
    "char_end",
    "chunk_id",
    "parser_version",
    "chunker_version",
    "embedding_model",
    "embedding_version",
    "index_version",
)


class EvidenceErrorCode(StrEnum):
    """Explicit reasons a hit cannot become evidence."""

    MISSING_PAGE_METADATA = "missing_page_metadata"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    STALE_INDEX = "stale_index"
    INVALID_METADATA = "invalid_metadata"
    UNKNOWN_PAGE = "unknown_page"
    COORDINATES_OUT_OF_RANGE = "coordinates_out_of_range"
    CHUNK_ID_MISMATCH = "chunk_id_mismatch"
    QUOTE_MISMATCH = "quote_mismatch"
    EMPTY_QUOTE = "empty_quote"


class EvidenceResolutionError(RuntimeError):
    """A retrieved chunk that cannot be resolved to a verifiable page quote."""

    def __init__(
        self,
        code: EvidenceErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class PageChunk:
    """One retrieval chunk that stays inside a single PDF page."""

    chunk_id: str
    page_number: int
    char_start: int
    char_end: int
    text: str


def sha256_file(path: str | Path) -> str:
    """Hash a file's bytes without loading it into memory at once."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(_READ_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalize_page_text(text: str) -> str:
    """Return the ``normalized_page_text_v1`` form of one page.

    Rules (frozen): unify line endings, collapse intra-line whitespace runs,
    drop blank lines except a single separator between paragraphs and strip the
    page edges.  Character coordinates in stored chunks and evidence spans index
    into this string, never into the raw extraction output.
    """

    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in unified.split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept).strip()


def normalized_pages(pages: Iterable[tuple[int, str]]) -> list[tuple[int, str]]:
    """Normalize ``(page_number, raw_text)`` pairs and drop empty pages."""

    normalized: list[tuple[int, str]] = []
    for page_number, raw_text in pages:
        text = normalize_page_text(raw_text)
        if text:
            normalized.append((page_number, text))
    return normalized


def chunk_id_for(
    artifact_sha256: str,
    page_number: int,
    char_start: int,
    char_end: int,
    *,
    chunker_version: str = CHUNKER_VERSION,
) -> str:
    """Deterministic chunk identity bound to the artifact, page and offsets."""

    payload = f"{artifact_sha256}:{page_number}:{char_start}:{char_end}:{chunker_version}"
    return f"chunk_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def index_fingerprint(
    *,
    artifact_sha256: str,
    parser_version: str = PARSER_VERSION,
    chunker_version: str = CHUNKER_VERSION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_version: str = DEFAULT_EMBEDDING_VERSION,
    index_version: str = INDEX_VERSION,
) -> str:
    """Fingerprint of every input that makes an index entry valid.

    A stored entry whose fingerprint differs from the current one is stale and
    must be rebuilt before it can produce evidence.
    """

    payload = "|".join(
        (
            artifact_sha256,
            parser_version,
            chunker_version,
            embedding_model,
            embedding_version,
            index_version,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def page_slices(
    text: str,
    *,
    target_chars: int = CHUNK_TARGET_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[tuple[int, int]]:
    """Split one normalized page into overlapping windows that never cross it.

    ``overlap_chars`` must stay well below ``target_chars`` (the frozen defaults
    are 180/1400); degenerate ratios close to 1:1 make windows advance by a few
    characters at a time and are not part of the supported contract.
    """

    if target_chars < 1:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be smaller than target_chars")
    length = len(text)
    if length == 0:
        return []
    if length <= target_chars:
        return [(0, length)]

    slices: list[tuple[int, int]] = []
    position = 0
    while position < length:
        end = min(position + target_chars, length)
        if end < length:
            window = text[position:end]
            for separator in _BOUNDARY_PREFERENCES:
                cut = window.rfind(separator)
                if cut >= target_chars // 2:
                    end = position + cut + len(separator)
                    break
        slices.append((position, end))
        if end >= length:
            break
        position = max(end - overlap_chars, position + 1)
    return slices


def build_page_chunks(
    pages: Iterable[tuple[int, str]],
    *,
    artifact_sha256: str,
    target_chars: int = CHUNK_TARGET_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
    chunker_version: str = CHUNKER_VERSION,
) -> list[PageChunk]:
    """Build page-scoped chunks with exact coordinates in normalized page text."""

    chunks: list[PageChunk] = []
    for page_number, text in normalized_pages(pages):
        for char_start, char_end in page_slices(
            text,
            target_chars=target_chars,
            overlap_chars=overlap_chars,
        ):
            quote = text[char_start:char_end]
            if not quote.strip():
                continue
            chunks.append(
                PageChunk(
                    chunk_id=chunk_id_for(
                        artifact_sha256,
                        page_number,
                        char_start,
                        char_end,
                        chunker_version=chunker_version,
                    ),
                    page_number=page_number,
                    char_start=char_start,
                    char_end=char_end,
                    text=quote,
                )
            )
    return chunks


def chunk_metadata(
    chunk: PageChunk,
    *,
    doc_id: str,
    title: str,
    chunk_index: int,
    artifact_sha256: str,
    index_fingerprint_value: str,
    project_root: str | None = None,
    source_id: str | None = None,
    parser_version: str = PARSER_VERSION,
    chunker_version: str = CHUNKER_VERSION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_version: str = DEFAULT_EMBEDDING_VERSION,
    index_version: str = INDEX_VERSION,
) -> dict[str, str | int]:
    """Stored Chroma metadata for one page chunk.

    Only ``str`` and ``int`` values are allowed, because that is what the vector
    store accepts; absent scoping values are omitted rather than stored as null.
    """

    metadata: dict[str, str | int] = {
        "doc_id": doc_id,
        "title": title,
        "chunk_index": chunk_index,
        "source_kind": "literature_page",
        "chunk_id": chunk.chunk_id,
        "artifact_sha256": artifact_sha256,
        "page_start": chunk.page_number,
        "page_end": chunk.page_number,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "parser_version": parser_version,
        "chunker_version": chunker_version,
        "embedding_model": embedding_model,
        "embedding_version": embedding_version,
        "index_version": index_version,
        "index_fingerprint": index_fingerprint_value,
    }
    if project_root is not None:
        metadata["project_root"] = project_root
    if source_id is not None:
        metadata["source_id"] = source_id
    return metadata


def resolve_evidence_span(
    *,
    source_id: str,
    artifact_sha256: str,
    page_texts: Mapping[int, str],
    page_number: int,
    char_start: int,
    char_end: int,
    chunk_id: str,
    parser_version: str = PARSER_VERSION,
    chunker_version: str = CHUNKER_VERSION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_version: str = DEFAULT_EMBEDDING_VERSION,
    index_version: str = INDEX_VERSION,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
) -> EvidenceSpan:
    """Derive a verifiable evidence span from stored chunk coordinates.

    The quote is read *from* the page text at the stored coordinates; a caller
    supplied quote is never trusted.  Every mismatch raises
    :class:`EvidenceResolutionError` instead of returning a weaker span.
    """

    normalized_hash = artifact_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(normalized_hash):
        raise EvidenceResolutionError(
            EvidenceErrorCode.INVALID_METADATA,
            "chunk metadata does not carry a valid artifact SHA-256",
            details={"artifact_sha256": artifact_sha256},
        )
    if context_chars < 0:
        raise ValueError("context_chars cannot be negative")
    page_text = page_texts.get(page_number)
    if page_text is None:
        raise EvidenceResolutionError(
            EvidenceErrorCode.UNKNOWN_PAGE,
            "chunk page is not present in the parsed artifact",
            details={"page_number": page_number, "artifact_sha256": normalized_hash},
        )
    if char_start < 0 or char_end > len(page_text) or char_end <= char_start:
        raise EvidenceResolutionError(
            EvidenceErrorCode.COORDINATES_OUT_OF_RANGE,
            "chunk coordinates fall outside the normalized page text",
            details={
                "page_number": page_number,
                "char_start": char_start,
                "char_end": char_end,
                "page_chars": len(page_text),
            },
        )
    quote = page_text[char_start:char_end]
    if not quote.strip():
        raise EvidenceResolutionError(
            EvidenceErrorCode.EMPTY_QUOTE,
            "chunk coordinates do not select any quotable text",
            details={"page_number": page_number, "char_start": char_start, "char_end": char_end},
        )
    expected_chunk_id = chunk_id_for(
        normalized_hash,
        page_number,
        char_start,
        char_end,
        chunker_version=chunker_version,
    )
    if expected_chunk_id != chunk_id:
        raise EvidenceResolutionError(
            EvidenceErrorCode.CHUNK_ID_MISMATCH,
            "chunk identity does not match the artifact, page and coordinates",
            details={"chunk_id": chunk_id, "expected_chunk_id": expected_chunk_id},
        )

    return EvidenceSpan(
        source_id=source_id,
        artifact_sha256=normalized_hash,
        page_start=page_number,
        page_end=page_number,
        char_start=char_start,
        char_end=char_end,
        chunk_id=chunk_id,
        exact_quote=quote,
        context_before=page_text[max(0, char_start - context_chars) : char_start],
        context_after=page_text[char_end : char_end + context_chars],
        parser_version=parser_version,
        chunker_version=chunker_version,
        embedding_model=embedding_model,
        embedding_version=embedding_version,
        index_version=index_version,
    )


def evidence_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the page-evidence fields from a retrieval hit's metadata.

    ``page_end`` is required as well but is not part of the returned projection:
    it is only used to enforce the single-page rule, so a span can never quietly
    span several pages.  Translation chunks written by the older text pipeline
    carry no page metadata; they are rejected here so they can never be rendered
    as evidence.
    """

    missing = [key for key in (*EVIDENCE_METADATA_KEYS, "page_end") if metadata.get(key) is None]
    if missing:
        raise EvidenceResolutionError(
            EvidenceErrorCode.MISSING_PAGE_METADATA,
            "retrieval hit has no page-level evidence metadata",
            details={"missing_keys": missing, "doc_id": metadata.get("doc_id")},
        )
    page_start = metadata["page_start"]
    page_end = metadata["page_end"]
    if page_start != page_end:
        raise EvidenceResolutionError(
            EvidenceErrorCode.INVALID_METADATA,
            "first-version evidence cannot span several pages",
            details={"page_start": page_start, "page_end": page_end},
        )
    for key in ("page_start", "char_start", "char_end"):
        if not isinstance(metadata[key], int) or isinstance(metadata[key], bool):
            raise EvidenceResolutionError(
                EvidenceErrorCode.INVALID_METADATA,
                "chunk coordinates must be integers",
                details={"key": key, "value": metadata[key]},
            )
    return {key: metadata[key] for key in EVIDENCE_METADATA_KEYS}


def assert_current_versions(
    metadata: Mapping[str, Any],
    *,
    parser_version: str = PARSER_VERSION,
    chunker_version: str = CHUNKER_VERSION,
    index_version: str = INDEX_VERSION,
    embedding_model: str | None = None,
    embedding_version: str | None = None,
) -> None:
    """Refuse metadata produced by a different parser/chunker/embedding config.

    A version bump changes how text or vectors are produced, so an entry created
    before the bump must be rebuilt instead of being served as evidence.
    """

    expected: dict[str, Any] = {
        "parser_version": parser_version,
        "chunker_version": chunker_version,
        "index_version": index_version,
    }
    if embedding_model is not None:
        expected["embedding_model"] = embedding_model
    if embedding_version is not None:
        expected["embedding_version"] = embedding_version

    stale = {
        key: {"stored": metadata.get(key), "current": value}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if stale:
        raise EvidenceResolutionError(
            EvidenceErrorCode.STALE_INDEX,
            "index was built with a different parser, chunker or embedding configuration",
            details={"stale_fields": stale, "doc_id": metadata.get("doc_id")},
        )


__all__ = [
    "CHUNKER_VERSION",
    "CHUNK_OVERLAP_CHARS",
    "CHUNK_TARGET_CHARS",
    "DEFAULT_CONTEXT_CHARS",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_VERSION",
    "EVIDENCE_METADATA_KEYS",
    "INDEX_VERSION",
    "PARSER_VERSION",
    "EvidenceErrorCode",
    "EvidenceResolutionError",
    "PageChunk",
    "assert_current_versions",
    "build_page_chunks",
    "chunk_id_for",
    "chunk_metadata",
    "evidence_metadata",
    "index_fingerprint",
    "normalize_page_text",
    "normalized_pages",
    "page_slices",
    "resolve_evidence_span",
    "sha256_file",
]
