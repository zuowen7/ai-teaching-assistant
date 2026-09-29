"""Unit tests for the P2B page-aware evidence core.

The module under test owns the invariants of ``docs/literature-research-poc-plan.md``
sections 4.3 and 4.5: a retrieval chunk never crosses a PDF page, every chunk
carries the artifact SHA-256, its page and its character coordinates in
``normalized_page_text_v1`` space, and an evidence span is *derived* from those
coordinates instead of being trusted.

These tests pin the frozen coordinate space, the page boundary behavior, the
deterministic identity of stored chunks and the explicit failure codes callers
switch on.  Everything is offline and deterministic: no network, no vector
store, no PDF parsing.
"""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

import pytest

from src.literature.evidence import (
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    CHUNKER_VERSION,
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_VERSION,
    EVIDENCE_METADATA_KEYS,
    INDEX_VERSION,
    PARSER_VERSION,
    EvidenceErrorCode,
    EvidenceResolutionError,
    PageChunk,
    build_page_chunks,
    chunk_id_for,
    chunk_metadata,
    evidence_metadata,
    index_fingerprint,
    normalize_page_text,
    normalized_pages,
    page_slices,
    resolve_evidence_span,
    sha256_file,
)
from src.literature.models import EvidenceSpan

ARTIFACT_SHA256 = hashlib.sha256(b"page-aware evidence fixture").hexdigest()
OTHER_ARTIFACT_SHA256 = hashlib.sha256(b"another artifact").hexdigest()

# One page of already-normalized text; ``QUOTE`` is a full paragraph inside it.
PAGE_NUMBER = 7
PAGE_TEXT = "Alpha paragraph one.\n\nBeta paragraph two with more words.\n\nGamma tail."
QUOTE = "Beta paragraph two with more words."
QUOTE_START = PAGE_TEXT.index(QUOTE)
QUOTE_END = QUOTE_START + len(QUOTE)

# Already normalized: single spaces, single trailing-space-free line, no CR.
LONG_PAGE_TEXT = (
    "Paragraph with a moderate amount of text to cross the chunk target. " * 45
).rstrip()

PAGE_ONE_TAIL = "The method continues"
PAGE_TWO_HEAD = "on the next page."
BOUNDARY_PHRASE = f"{PAGE_ONE_TAIL}\n\n{PAGE_TWO_HEAD}"

_NORMALIZATION_FRAGMENTS = (
    "alpha",
    " ",
    "  ",
    "\t",
    "\r\n",
    "\n",
    "\n\n",
    "\n\n\n",
    "中文",
    "café",
    ".",
)

_SLICE_UNITS = ("alpha beta. ", "gamma\n\n", "中文句子。", "x ", "tail\r\n")


def _normalization_corpus() -> tuple[str, ...]:
    """Curated and combinatorially generated pages covering the v1 rules."""

    texts = [
        "",
        " ",
        "\r",
        "\n\n\n",
        "  \t \r\n ",
        "single line",
        "alpha\r\nbeta\rgamma\ndelta",
        "trailing spaces   ",
        "   leading spaces",
        "para one\n\npara two",
        "para one\n\n\n\n\npara two",
        "line \n \n \n line",
        "中文   测试",
        "alpha\u00a0\u00a0beta",
        "alpha beta. " * 500,
        "para\n\n" * 200,
        "x" * 5000,
    ]
    texts.extend("".join(parts) for parts in itertools.product(_NORMALIZATION_FRAGMENTS, repeat=3))
    return tuple(texts)


def _slice_corpus() -> tuple[str, ...]:
    """Normalized pages of mixed length, script and paragraph structure."""

    texts = [
        "a",
        "short page",
        "Page with a few sentences. " * 40,
        "Paragraph text to cross the target. " * 45,
        "Sentence with a boundary. " * 120,
        "para\n\n" * 300,
        "中文段落。" * 400,
    ]
    texts.extend(
        normalize_page_text("".join(parts) * 60)
        for parts in itertools.product(_SLICE_UNITS, repeat=2)
    )
    return tuple(texts)


NORMALIZATION_CORPUS = _normalization_corpus()
SLICE_CORPUS = _slice_corpus()


def boundary_document() -> list[tuple[int, str]]:
    """Two raw pages whose flattened text would obviously span the boundary."""

    return [
        (1, ("Page one body   sentence. " * 80) + PAGE_ONE_TAIL),
        (2, PAGE_TWO_HEAD + (" Page two body sentence. " * 80)),
    ]


def resolve_kwargs(**overrides: object) -> dict[str, object]:
    """Well-formed resolver arguments for ``PAGE_TEXT``; overrides break one field."""

    payload: dict[str, object] = {
        "source_id": "src_local_7",
        "artifact_sha256": ARTIFACT_SHA256,
        "page_texts": {PAGE_NUMBER: PAGE_TEXT},
        "page_number": PAGE_NUMBER,
        "char_start": QUOTE_START,
        "char_end": QUOTE_END,
        "chunk_id": chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, QUOTE_START, QUOTE_END),
    }
    payload.update(overrides)
    return payload


def make_metadata(**overrides: object) -> dict[str, object]:
    """A page-aware retrieval hit as written by :func:`chunk_metadata`."""

    payload: dict[str, object] = {
        "doc_id": "doc_literature_1",
        "title": "Page-aware retrieval",
        "chunk_index": 0,
        "source_kind": "literature_page",
        "artifact_sha256": ARTIFACT_SHA256,
        "page_start": 3,
        "page_end": 3,
        "char_start": 0,
        "char_end": len(QUOTE),
        "chunk_id": chunk_id_for(ARTIFACT_SHA256, 3, 0, len(QUOTE)),
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_version": DEFAULT_EMBEDDING_VERSION,
        "index_version": INDEX_VERSION,
        "index_fingerprint": index_fingerprint(artifact_sha256=ARTIFACT_SHA256),
    }
    payload.update(overrides)
    return payload


class TestNormalizePageText:
    def test_unifies_crlf_and_cr_line_endings(self) -> None:
        assert normalize_page_text("alpha\r\nbeta\rgamma\ndelta") == "alpha\nbeta\ngamma\ndelta"
        assert normalize_page_text("para one\r\n\r\npara two") == "para one\n\npara two"
        assert normalize_page_text("para one\r\r\rpara two") == "para one\n\npara two"
        assert "\r" not in normalize_page_text("a\r\nb\rc")

    def test_collapses_intra_line_whitespace_runs_to_one_space(self) -> None:
        assert normalize_page_text("alpha   beta\t\tgamma") == "alpha beta gamma"
        assert normalize_page_text("alpha\x0bbeta\x0cgamma") == "alpha beta gamma"
        assert normalize_page_text("alpha\u00a0\u00a0beta") == "alpha beta"
        assert normalize_page_text("中文\u3000\u3000测试") == "中文 测试"

    def test_removes_line_trailing_whitespace_and_page_edges(self) -> None:
        assert normalize_page_text("alpha   \n   beta") == "alpha\nbeta"
        assert normalize_page_text("\n\n   alpha   \n\n") == "alpha"
        assert normalize_page_text("  alpha\n\nbeta  ") == "alpha\n\nbeta"

    def test_collapses_blank_line_runs_to_a_single_blank_line(self) -> None:
        assert normalize_page_text("alpha\n\nbeta") == "alpha\n\nbeta"
        assert normalize_page_text("alpha\n\n\n\n\nbeta") == "alpha\n\nbeta"
        assert normalize_page_text("alpha\n \n\t\nbeta") == "alpha\n\nbeta"
        assert "\n\n\n" not in normalize_page_text("alpha\n\n\n\nbeta")

    def test_whitespace_only_page_normalizes_to_empty(self) -> None:
        for index, text in enumerate(("", " ", "\r", "\n\n\n", "  \t \r\n ", "\u3000\n\u00a0")):
            assert normalize_page_text(text) == "", f"corpus item {index}"

    def test_preserves_unicode_letters_and_punctuation(self) -> None:
        raw = "中文段落   第一行。\n\n第二行：café — naïve  \n\n\n Ω"
        assert normalize_page_text(raw) == "中文段落 第一行。\n\n第二行：café — naïve\n\nΩ"
        assert (
            normalize_page_text("  línea uno\r\n\r\n\r\n  línea   dos \t Ω  ")
            == "línea uno\n\nlínea dos Ω"
        )

    def test_normalization_is_idempotent_and_structurally_stable(self) -> None:
        for text in NORMALIZATION_CORPUS:
            normalized = normalize_page_text(text)

            assert normalize_page_text(normalized) == normalized
            assert normalized == normalized.strip()
            assert "\r" not in normalized
            assert "\n\n\n" not in normalized
            # Only plain single spaces and newlines survive normalization.
            assert not any(char.isspace() and char not in {" ", "\n"} for char in normalized)
            for line in normalized.split("\n"):
                assert line == line.strip()
                assert "  " not in line


class TestNormalizedPages:
    def test_keeps_page_numbers_and_input_order(self) -> None:
        pages = [(3, "third   page"), (5, "fifth page"), (9, "ninth page")]

        assert normalized_pages(pages) == [(3, "third page"), (5, "fifth page"), (9, "ninth page")]
        assert [number for number, _ in normalized_pages(pages)] == [3, 5, 9]

    def test_drops_pages_that_normalize_to_empty(self) -> None:
        pages = [(1, "  \r\n  "), (2, "kept"), (3, "\n\n\n"), (4, "")]

        assert normalized_pages(pages) == [(2, "kept")]

    def test_normalizes_the_text_of_kept_pages(self) -> None:
        pages = [(2, "  alpha\r\n\r\n\r\nbeta   gamma ")]

        assert normalized_pages(pages) == [(2, "alpha\n\nbeta gamma")]

    def test_consumes_a_lazy_iterable_and_can_return_nothing(self) -> None:
        lazy = ((number, text) for number, text in [(1, " "), (2, "text")])

        assert normalized_pages(lazy) == [(2, "text")]
        assert normalized_pages([]) == []


class TestPageSlices:
    def test_empty_text_yields_no_slices(self) -> None:
        assert page_slices("") == []

    def test_page_shorter_than_target_yields_one_slice_covering_it(self) -> None:
        for text in ("a", "short page text", LONG_PAGE_TEXT[: CHUNK_TARGET_CHARS - 1]):
            assert page_slices(text) == [(0, len(text))]

    def test_forwards_custom_target_and_overlap(self) -> None:
        slices = page_slices(LONG_PAGE_TEXT, target_chars=200, overlap_chars=40)

        assert len(slices) >= 3
        assert slices[0][0] == 0
        assert slices[-1][1] == len(LONG_PAGE_TEXT)
        for start, end in slices:
            assert 0 < end - start <= 200
        for (start, end), (next_start, next_end) in itertools.pairwise(slices):
            assert next_start > start
            assert next_end > end
            assert next_start <= end
            assert end - next_start <= 40

    def test_long_page_slices_advance_strictly_and_overlap_within_the_limit(self) -> None:
        slices = page_slices(LONG_PAGE_TEXT)

        assert len(slices) >= 3
        assert slices[0][0] == 0
        assert slices[-1][1] == len(LONG_PAGE_TEXT)
        starts = [start for start, _ in slices]
        ends = [end for _, end in slices]
        assert all(later > earlier for earlier, later in itertools.pairwise(starts))
        assert all(later > earlier for earlier, later in itertools.pairwise(ends))
        for (start, end), (next_start, next_end) in itertools.pairwise(slices):
            assert end > start
            assert next_end > next_start
            assert end - start <= CHUNK_TARGET_CHARS
            assert next_start <= end  # consecutive slices never leave a gap
            assert end - next_start <= CHUNK_OVERLAP_CHARS
            assert next_start > start  # the loop always advances

    def test_slice_properties_hold_across_generated_pages(self) -> None:
        for text in SLICE_CORPUS:
            slices = page_slices(text)

            if not text:
                assert slices == []
                continue

            assert slices[0][0] == 0
            assert slices[-1][1] == len(text)
            previous_start, previous_end = slices[0]
            assert previous_end > previous_start
            assert previous_end - previous_start <= CHUNK_TARGET_CHARS
            for start, end in slices[1:]:
                assert end > start
                assert end > previous_end
                assert start > previous_start
                assert end - start <= CHUNK_TARGET_CHARS
                assert start <= previous_end
                assert previous_end - start <= CHUNK_OVERLAP_CHARS
                previous_start, previous_end = start, end

    @pytest.mark.parametrize(
        ("target_chars", "overlap_chars", "message"),
        [
            (0, 0, "target_chars must be positive"),
            (-1, 0, "target_chars must be positive"),
            (10, 10, "overlap_chars must be smaller than target_chars"),
            (10, 11, "overlap_chars must be smaller than target_chars"),
            (10, -1, "overlap_chars must be smaller than target_chars"),
        ],
    )
    def test_invalid_arguments_raise_value_error(
        self, target_chars: int, overlap_chars: int, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            page_slices(LONG_PAGE_TEXT, target_chars=target_chars, overlap_chars=overlap_chars)

    def test_arguments_are_validated_even_for_an_empty_page(self) -> None:
        with pytest.raises(ValueError, match="target_chars must be positive"):
            page_slices("", target_chars=0, overlap_chars=0)
        with pytest.raises(ValueError, match="overlap_chars must be smaller"):
            page_slices("", target_chars=10, overlap_chars=10)


class TestBuildPageChunks:
    def test_every_chunk_round_trips_inside_its_own_page(self) -> None:
        raw_pages = boundary_document()
        normalized = dict(normalized_pages(raw_pages))
        chunks = build_page_chunks(raw_pages, artifact_sha256=ARTIFACT_SHA256)

        assert chunks
        assert {chunk.page_number for chunk in chunks} == {1, 2}
        for chunk in chunks:
            assert isinstance(chunk, PageChunk)
            assert chunk.page_number in normalized
            page_text = normalized[chunk.page_number]
            assert chunk.char_end > chunk.char_start >= 0
            assert chunk.char_end <= len(page_text)
            assert page_text[chunk.char_start : chunk.char_end] == chunk.text
            assert chunk.text.strip()
            assert chunk.chunk_id == chunk_id_for(
                ARTIFACT_SHA256, chunk.page_number, chunk.char_start, chunk.char_end
            )

    def test_chunks_are_grouped_by_page_and_cover_each_page_in_order(self) -> None:
        raw_pages = boundary_document()
        normalized = dict(normalized_pages(raw_pages))
        chunks = build_page_chunks(raw_pages, artifact_sha256=ARTIFACT_SHA256)

        assert [chunk.page_number for chunk in chunks] == sorted(
            chunk.page_number for chunk in chunks
        )
        for page_number, page_text in normalized.items():
            page_chunks = [chunk for chunk in chunks if chunk.page_number == page_number]
            assert page_chunks
            assert page_chunks[0].char_start == 0
            assert page_chunks[-1].char_end == len(page_text)
            starts = [chunk.char_start for chunk in page_chunks]
            assert starts == sorted(starts)
            assert len(set(starts)) == len(starts)
            for previous, current in itertools.pairwise(page_chunks):
                assert current.char_start <= previous.char_end
                assert previous.char_end - current.char_start <= CHUNK_OVERLAP_CHARS

    def test_multi_page_document_never_spans_a_page_boundary(self) -> None:
        raw_pages = boundary_document()
        normalized = dict(normalized_pages(raw_pages))
        page_one_text = normalized[1]
        page_two_text = normalized[2]
        flattened = f"{page_one_text}\n\n{page_two_text}"

        # A chunker over the flattened document would happily emit this phrase.
        assert BOUNDARY_PHRASE in flattened

        chunks = build_page_chunks(raw_pages, artifact_sha256=ARTIFACT_SHA256)
        page_one_chunks = [chunk for chunk in chunks if chunk.page_number == 1]
        page_two_chunks = [chunk for chunk in chunks if chunk.page_number == 2]

        assert page_one_chunks and page_two_chunks
        assert all(BOUNDARY_PHRASE not in chunk.text for chunk in chunks)
        assert all(PAGE_TWO_HEAD not in chunk.text for chunk in page_one_chunks)
        assert all(PAGE_ONE_TAIL not in chunk.text for chunk in page_two_chunks)
        assert page_one_chunks[-1].text.endswith(PAGE_ONE_TAIL)
        assert max(chunk.char_end for chunk in page_one_chunks) == len(page_one_text)
        assert min(chunk.char_start for chunk in page_two_chunks) == 0

    def test_empty_normalized_page_produces_no_chunks(self) -> None:
        pages = [(1, "  \r\n\t "), (2, "real   text"), (3, "\n\n\n")]

        chunks = build_page_chunks(pages, artifact_sha256=ARTIFACT_SHA256)

        assert [chunk.page_number for chunk in chunks] == [2]
        assert chunks[0].text == "real text"
        assert (chunks[0].char_start, chunks[0].char_end) == (0, len("real text"))

    def test_short_page_produces_one_chunk_covering_it(self) -> None:
        chunks = build_page_chunks(
            [(4, "  Short   page.\r\n\r\n")], artifact_sha256=ARTIFACT_SHA256
        )

        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.page_number == 4
        assert (chunk.char_start, chunk.char_end) == (0, len("Short page."))
        assert chunk.text == "Short page."

    def test_chunk_ids_are_deterministic_and_reproducible(self) -> None:
        pages = boundary_document()

        first = build_page_chunks(pages, artifact_sha256=ARTIFACT_SHA256)
        second = build_page_chunks(list(pages), artifact_sha256=ARTIFACT_SHA256)

        assert first == second
        assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
        assert len({chunk.chunk_id for chunk in first}) == len(first)

    def test_chunk_ids_depend_on_artifact_page_offsets_and_chunker_version(self) -> None:
        raw_page = "Shared page text repeated. " * 80
        baseline = build_page_chunks([(1, raw_page)], artifact_sha256=ARTIFACT_SHA256)
        assert len(baseline) >= 2

        other_artifact = build_page_chunks([(1, raw_page)], artifact_sha256=OTHER_ARTIFACT_SHA256)
        assert [chunk.text for chunk in other_artifact] == [chunk.text for chunk in baseline]
        assert [chunk.chunk_id for chunk in other_artifact] != [
            chunk.chunk_id for chunk in baseline
        ]

        other_page = build_page_chunks([(2, raw_page)], artifact_sha256=ARTIFACT_SHA256)
        assert [chunk.char_start for chunk in other_page] == [
            chunk.char_start for chunk in baseline
        ]
        assert [chunk.chunk_id for chunk in other_page] != [chunk.chunk_id for chunk in baseline]

        other_chunker = build_page_chunks(
            [(1, raw_page)], artifact_sha256=ARTIFACT_SHA256, chunker_version="page-chunker-v2"
        )
        assert [chunk.chunk_id for chunk in other_chunker] != [chunk.chunk_id for chunk in baseline]

        other_offsets = build_page_chunks(
            [(1, raw_page)], artifact_sha256=ARTIFACT_SHA256, target_chars=300, overlap_chars=40
        )
        assert [chunk.char_start for chunk in other_offsets] != [
            chunk.char_start for chunk in baseline
        ]
        assert {chunk.chunk_id for chunk in other_offsets}.isdisjoint(
            {chunk.chunk_id for chunk in baseline}
        )

        assert chunk_id_for(ARTIFACT_SHA256, 1, 0, 10) != chunk_id_for(ARTIFACT_SHA256, 1, 0, 11)
        assert chunk_id_for(ARTIFACT_SHA256, 1, 0, 10) != chunk_id_for(ARTIFACT_SHA256, 1, 1, 11)
        assert chunk_id_for(ARTIFACT_SHA256, 1, 0, 10) != chunk_id_for(
            ARTIFACT_SHA256, 1, 0, 10, chunker_version="page-chunker-v2"
        )


class TestIndexFingerprint:
    def test_equal_inputs_produce_the_same_fingerprint(self) -> None:
        explicit = index_fingerprint(
            artifact_sha256=ARTIFACT_SHA256,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_version=DEFAULT_EMBEDDING_VERSION,
            index_version=INDEX_VERSION,
        )

        assert index_fingerprint(artifact_sha256=ARTIFACT_SHA256) == explicit
        assert index_fingerprint(artifact_sha256=ARTIFACT_SHA256) == index_fingerprint(
            artifact_sha256=ARTIFACT_SHA256
        )
        assert index_fingerprint(artifact_sha256=OTHER_ARTIFACT_SHA256) != explicit

    @pytest.mark.parametrize(
        "overrides",
        [
            {"artifact_sha256": OTHER_ARTIFACT_SHA256},
            {"parser_version": "parser-v2"},
            {"chunker_version": "page-chunker-v2"},
            {"embedding_model": "all-MiniLM-L6-v2"},
            {"embedding_version": "sentence-transformers-1"},
            {"index_version": "index-v2"},
        ],
    )
    def test_every_index_input_changes_the_fingerprint(self, overrides: dict[str, str]) -> None:
        baseline_kwargs = {"artifact_sha256": ARTIFACT_SHA256}

        baseline = index_fingerprint(**baseline_kwargs)
        changed = index_fingerprint(**{**baseline_kwargs, **overrides})

        assert changed != baseline

    def test_fingerprint_is_a_short_lowercase_hex_digest(self) -> None:
        fingerprint = index_fingerprint(artifact_sha256=ARTIFACT_SHA256)

        assert len(fingerprint) == 24
        assert fingerprint == fingerprint.lower()
        assert all(char in "0123456789abcdef" for char in fingerprint)


class TestResolveEvidenceSpan:
    def test_happy_path_derives_the_quote_from_the_page_text(self) -> None:
        span = resolve_evidence_span(**resolve_kwargs())

        assert isinstance(span, EvidenceSpan)
        assert span.source_id == "src_local_7"
        assert span.artifact_sha256 == ARTIFACT_SHA256
        assert span.page_start == PAGE_NUMBER
        assert span.page_end == PAGE_NUMBER
        assert span.coordinate_space == "normalized_page_text_v1"
        assert (span.char_start, span.char_end) == (QUOTE_START, QUOTE_END)
        assert span.exact_quote == QUOTE
        assert span.exact_quote == PAGE_TEXT[span.char_start : span.char_end]
        assert span.chunk_id == chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, QUOTE_START, QUOTE_END)
        assert span.quote_sha256 == hashlib.sha256(QUOTE.encode("utf-8")).hexdigest()
        assert span.evidence_id.startswith("evidence_")
        assert len(span.evidence_id) == len("evidence_") + 24
        assert span.parser_version == PARSER_VERSION
        assert span.chunker_version == CHUNKER_VERSION
        assert span.embedding_model == DEFAULT_EMBEDDING_MODEL
        assert span.embedding_version == DEFAULT_EMBEDDING_VERSION
        assert span.index_version == INDEX_VERSION

    def test_context_is_bounded_by_context_chars_and_matches_the_page(self) -> None:
        span = resolve_evidence_span(**resolve_kwargs(context_chars=10))

        assert span.context_before == PAGE_TEXT[QUOTE_START - 10 : QUOTE_START]
        assert span.context_after == PAGE_TEXT[QUOTE_END : QUOTE_END + 10]
        assert len(span.context_before) == 10
        assert len(span.context_after) == 10
        assert (
            span.context_before + span.exact_quote + span.context_after
            == PAGE_TEXT[QUOTE_START - 10 : QUOTE_END + 10]
        )

    def test_context_is_clamped_at_the_page_edges(self) -> None:
        first_paragraph = "Alpha paragraph one."
        last_paragraph = "Gamma tail."
        opening = resolve_evidence_span(
            **resolve_kwargs(
                char_start=0,
                char_end=len(first_paragraph),
                chunk_id=chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, 0, len(first_paragraph)),
                context_chars=100,
            )
        )
        closing = resolve_evidence_span(
            **resolve_kwargs(
                char_start=PAGE_TEXT.index(last_paragraph),
                char_end=len(PAGE_TEXT),
                chunk_id=chunk_id_for(
                    ARTIFACT_SHA256, PAGE_NUMBER, PAGE_TEXT.index(last_paragraph), len(PAGE_TEXT)
                ),
                context_chars=100,
            )
        )

        assert opening.context_before == ""
        assert opening.context_after == PAGE_TEXT[len(first_paragraph) : len(first_paragraph) + 100]
        assert len(opening.context_after) <= 100
        assert closing.context_after == ""
        assert closing.context_before == PAGE_TEXT[: PAGE_TEXT.index(last_paragraph)]
        assert len(closing.context_before) <= 100

    def test_default_context_window_is_default_context_chars(self) -> None:
        # A quote in the middle of a long page, so the default window really binds.
        char_start = 1500
        char_end = char_start + 40
        span = resolve_evidence_span(
            source_id="src_local_7",
            artifact_sha256=ARTIFACT_SHA256,
            page_texts={PAGE_NUMBER: LONG_PAGE_TEXT},
            page_number=PAGE_NUMBER,
            char_start=char_start,
            char_end=char_end,
            chunk_id=chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, char_start, char_end),
        )

        assert len(LONG_PAGE_TEXT) > char_end + DEFAULT_CONTEXT_CHARS
        assert span.exact_quote == LONG_PAGE_TEXT[char_start:char_end]
        assert (
            span.context_before == LONG_PAGE_TEXT[char_start - DEFAULT_CONTEXT_CHARS : char_start]
        )
        assert span.context_after == LONG_PAGE_TEXT[char_end : char_end + DEFAULT_CONTEXT_CHARS]
        assert len(span.context_before) == DEFAULT_CONTEXT_CHARS
        assert len(span.context_after) == DEFAULT_CONTEXT_CHARS

    def test_zero_context_chars_yields_empty_context(self) -> None:
        span = resolve_evidence_span(**resolve_kwargs(context_chars=0))

        assert span.context_before == ""
        assert span.context_after == ""
        assert span.exact_quote == QUOTE

    def test_artifact_hash_is_normalized_before_identity_is_checked(self) -> None:
        span = resolve_evidence_span(**resolve_kwargs(artifact_sha256=ARTIFACT_SHA256.upper()))

        assert span.artifact_sha256 == ARTIFACT_SHA256
        assert span.exact_quote == QUOTE

    def test_custom_versions_flow_into_the_span_and_the_chunk_identity(self) -> None:
        chunk_id = chunk_id_for(
            ARTIFACT_SHA256,
            PAGE_NUMBER,
            QUOTE_START,
            QUOTE_END,
            chunker_version="page-chunker-v2",
        )
        span = resolve_evidence_span(
            **resolve_kwargs(
                chunk_id=chunk_id,
                parser_version="parser-v2",
                chunker_version="page-chunker-v2",
                embedding_model="all-MiniLM-L6-v2",
                embedding_version="sentence-transformers-1",
                index_version="index-v2",
            )
        )

        assert span.chunk_id == chunk_id
        assert span.parser_version == "parser-v2"
        assert span.chunker_version == "page-chunker-v2"
        assert span.embedding_model == "all-MiniLM-L6-v2"
        assert span.embedding_version == "sentence-transformers-1"
        assert span.index_version == "index-v2"

    def test_evidence_id_is_stable_for_coordinates_and_changes_with_them(self) -> None:
        span = resolve_evidence_span(**resolve_kwargs())
        repeated = resolve_evidence_span(**resolve_kwargs())
        shifted = resolve_evidence_span(
            **resolve_kwargs(
                char_start=QUOTE_START + 1,
                char_end=QUOTE_END + 1,
                chunk_id=chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, QUOTE_START + 1, QUOTE_END + 1),
            )
        )

        assert span.evidence_id == repeated.evidence_id
        assert span.quote_sha256 == repeated.quote_sha256
        assert span.evidence_id != shifted.evidence_id

    @pytest.mark.parametrize(
        "artifact_sha256",
        ["", "not-a-hash", "a" * 63, "a" * 65, "g" * 64, f"{ARTIFACT_SHA256}0"],
    )
    def test_invalid_artifact_hash_is_rejected_with_invalid_metadata(
        self, artifact_sha256: str
    ) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            resolve_evidence_span(**resolve_kwargs(artifact_sha256=artifact_sha256))

        assert excinfo.value.code is EvidenceErrorCode.INVALID_METADATA
        assert excinfo.value.details["artifact_sha256"] == artifact_sha256

    def test_unknown_page_is_rejected(self) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            resolve_evidence_span(**resolve_kwargs(page_texts={PAGE_NUMBER + 1: PAGE_TEXT}))

        assert excinfo.value.code is EvidenceErrorCode.UNKNOWN_PAGE
        assert excinfo.value.details["page_number"] == PAGE_NUMBER
        assert excinfo.value.details["artifact_sha256"] == ARTIFACT_SHA256

    @pytest.mark.parametrize(
        ("char_start", "char_end"),
        [
            (-1, QUOTE_END),
            (QUOTE_START, len(PAGE_TEXT) + 1),
            (QUOTE_START, QUOTE_START),
            (QUOTE_START + 5, QUOTE_START),
            (len(PAGE_TEXT), len(PAGE_TEXT) + 1),
        ],
    )
    def test_out_of_range_coordinates_are_rejected(self, char_start: int, char_end: int) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            resolve_evidence_span(
                **resolve_kwargs(
                    char_start=char_start,
                    char_end=char_end,
                    chunk_id=chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, char_start, char_end),
                )
            )

        assert excinfo.value.code is EvidenceErrorCode.COORDINATES_OUT_OF_RANGE
        assert excinfo.value.details["page_chars"] == len(PAGE_TEXT)
        assert excinfo.value.details["char_start"] == char_start
        assert excinfo.value.details["char_end"] == char_end

    @pytest.mark.parametrize(
        "overrides",
        [
            {"chunker_version": "page-chunker-v2"},
            {"char_start": QUOTE_START + 1, "char_end": QUOTE_END + 1},
            {"page_number": PAGE_NUMBER + 1, "page_texts": {PAGE_NUMBER + 1: PAGE_TEXT}},
            {"artifact_sha256": OTHER_ARTIFACT_SHA256},
        ],
    )
    def test_chunk_identity_mismatch_is_rejected(self, overrides: dict[str, object]) -> None:
        effective = resolve_kwargs(**overrides)

        with pytest.raises(EvidenceResolutionError) as excinfo:
            resolve_evidence_span(**effective)

        assert excinfo.value.code is EvidenceErrorCode.CHUNK_ID_MISMATCH
        assert excinfo.value.details["chunk_id"] == resolve_kwargs()["chunk_id"]
        assert excinfo.value.details["expected_chunk_id"] == chunk_id_for(
            str(effective["artifact_sha256"]).strip().lower(),
            int(effective["page_number"]),
            int(effective["char_start"]),
            int(effective["char_end"]),
            chunker_version=str(effective.get("chunker_version", CHUNKER_VERSION)),
        )

    @pytest.mark.parametrize("marker", ["\n\n", " "])
    def test_whitespace_only_quote_is_rejected_with_empty_quote(self, marker: str) -> None:
        char_start = PAGE_TEXT.index(marker)
        char_end = char_start + len(marker)

        with pytest.raises(EvidenceResolutionError) as excinfo:
            resolve_evidence_span(
                **resolve_kwargs(
                    char_start=char_start,
                    char_end=char_end,
                    chunk_id=chunk_id_for(ARTIFACT_SHA256, PAGE_NUMBER, char_start, char_end),
                )
            )

        # The chunk identity is correct, so EMPTY_QUOTE is the reason reported.
        assert excinfo.value.code is EvidenceErrorCode.EMPTY_QUOTE
        assert excinfo.value.details["char_start"] == char_start
        assert excinfo.value.details["char_end"] == char_end


class TestEvidenceMetadata:
    def test_well_formed_metadata_returns_exactly_the_evidence_keys(self) -> None:
        metadata = make_metadata()

        result = evidence_metadata(metadata)

        assert set(result) == set(EVIDENCE_METADATA_KEYS)
        assert "page_end" not in result
        assert result == {key: metadata[key] for key in EVIDENCE_METADATA_KEYS}
        assert result["page_start"] == 3
        assert result["char_start"] == 0
        assert result["char_end"] == len(QUOTE)

    def test_metadata_produced_by_chunk_metadata_round_trips(self) -> None:
        chunk = build_page_chunks(
            [(5, "Page five body text. " * 40)], artifact_sha256=ARTIFACT_SHA256
        )[0]
        stored = chunk_metadata(
            chunk,
            doc_id="doc_literature_5",
            title="Stored chunk",
            chunk_index=3,
            artifact_sha256=ARTIFACT_SHA256,
            index_fingerprint_value=index_fingerprint(artifact_sha256=ARTIFACT_SHA256),
        )

        result = evidence_metadata(stored)

        assert set(result) == set(EVIDENCE_METADATA_KEYS)
        assert result["page_start"] == chunk.page_number
        assert result["chunk_id"] == chunk.chunk_id
        assert (result["char_start"], result["char_end"]) == (
            chunk.char_start,
            chunk.char_end,
        )
        assert result["artifact_sha256"] == ARTIFACT_SHA256

    def test_legacy_flat_text_chunk_is_rejected_with_missing_keys(self) -> None:
        legacy = {"doc_id": "doc_legacy", "title": "Translation chunk", "chunk_index": 4}

        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(legacy)

        assert excinfo.value.code is EvidenceErrorCode.MISSING_PAGE_METADATA
        assert set(excinfo.value.details["missing_keys"]) == {
            *EVIDENCE_METADATA_KEYS,
            "page_end",
        }
        assert excinfo.value.details["doc_id"] == "doc_legacy"

    def test_a_single_missing_key_is_named(self) -> None:
        metadata = make_metadata()
        del metadata["chunk_id"]

        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(metadata)

        assert excinfo.value.code is EvidenceErrorCode.MISSING_PAGE_METADATA
        assert excinfo.value.details["missing_keys"] == ["chunk_id"]

    def test_none_valued_keys_count_as_missing(self) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(make_metadata(index_version=None))

        assert excinfo.value.code is EvidenceErrorCode.MISSING_PAGE_METADATA
        assert excinfo.value.details["missing_keys"] == ["index_version"]

    def test_page_spanning_metadata_is_rejected(self) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(make_metadata(page_start=3, page_end=4))

        assert excinfo.value.code is EvidenceErrorCode.INVALID_METADATA
        assert excinfo.value.details["page_start"] == 3
        assert excinfo.value.details["page_end"] == 4

    def test_missing_page_end_cannot_be_read_as_a_page(self) -> None:
        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(make_metadata(page_end=None))

        # page_end is read during validation but is not part of the returned
        # projection, so a missing value must still fail structurally instead of
        # raising a bare KeyError.
        assert excinfo.value.code is EvidenceErrorCode.MISSING_PAGE_METADATA
        assert excinfo.value.details["missing_keys"] == ["page_end"]

    @pytest.mark.parametrize(
        ("overrides", "key"),
        [
            ({"page_start": "3", "page_end": "3"}, "page_start"),
            ({"page_start": 3.0, "page_end": 3.0}, "page_start"),
            ({"page_start": True, "page_end": True}, "page_start"),
            ({"char_start": True}, "char_start"),
            ({"char_start": 0.0}, "char_start"),
            ({"char_end": float(len(QUOTE))}, "char_end"),
            ({"char_end": "5"}, "char_end"),
        ],
    )
    def test_non_integer_coordinates_are_rejected(
        self, overrides: dict[str, object], key: str
    ) -> None:
        metadata = make_metadata(**overrides)

        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(metadata)

        assert excinfo.value.code is EvidenceErrorCode.INVALID_METADATA
        assert excinfo.value.details["key"] == key
        assert excinfo.value.details["value"] == metadata[key]

    def test_metadata_without_page_end_is_rejected_with_a_structured_error(self) -> None:
        """``page_end`` is validated by ``evidence_metadata`` but never presence-checked.

        ``EVIDENCE_METADATA_KEYS`` omits ``page_end`` while ``evidence_metadata``
        reads it directly, so a hit that carries all ten evidence keys but no
        ``page_end`` escapes the structured failure contract::

            metadata = {key: good[key] for key in EVIDENCE_METADATA_KEYS}
            evidence_metadata(metadata)  # KeyError: 'page_end'

        Callers dispatch on ``EvidenceResolutionError.code`` to decide between
        rebuilding an index and rendering evidence, so a missing page field must
        surface as a rejection code, never as an unhandled mapping lookup.
        """

        metadata = {key: make_metadata()[key] for key in EVIDENCE_METADATA_KEYS}
        assert set(metadata) == set(EVIDENCE_METADATA_KEYS)
        assert "page_end" not in metadata

        with pytest.raises(EvidenceResolutionError) as excinfo:
            evidence_metadata(metadata)

        assert excinfo.value.code in {
            EvidenceErrorCode.MISSING_PAGE_METADATA,
            EvidenceErrorCode.INVALID_METADATA,
        }


class TestChunkMetadata:
    def make_chunk(self) -> PageChunk:
        pages = [(5, "Page five   body text.\r\n\r\n" + ("More sentence. " * 30))]
        return build_page_chunks(pages, artifact_sha256=ARTIFACT_SHA256)[0]

    def make_stored(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "doc_id": "doc_literature_5",
            "title": "Stored chunk",
            "chunk_index": 0,
            "artifact_sha256": ARTIFACT_SHA256,
            "index_fingerprint_value": index_fingerprint(artifact_sha256=ARTIFACT_SHA256),
        }
        payload.update(overrides)
        return chunk_metadata(self.make_chunk(), **payload)

    def test_page_fields_agree_with_the_chunk(self) -> None:
        chunk = self.make_chunk()

        stored = self.make_stored(source_id="src_5", project_root="D:/project")

        assert stored["page_start"] == chunk.page_number
        assert stored["page_end"] == chunk.page_number
        assert stored["char_start"] == chunk.char_start
        assert stored["char_end"] == chunk.char_end
        assert stored["chunk_id"] == chunk.chunk_id
        assert stored["artifact_sha256"] == ARTIFACT_SHA256
        assert stored["doc_id"] == "doc_literature_5"
        assert stored["title"] == "Stored chunk"
        assert stored["chunk_index"] == 0
        assert stored["source_kind"] == "literature_page"
        assert stored["parser_version"] == PARSER_VERSION
        assert stored["chunker_version"] == CHUNKER_VERSION
        assert stored["embedding_model"] == DEFAULT_EMBEDDING_MODEL
        assert stored["embedding_version"] == DEFAULT_EMBEDDING_VERSION
        assert stored["index_version"] == INDEX_VERSION
        assert stored["index_fingerprint"] == index_fingerprint(artifact_sha256=ARTIFACT_SHA256)
        assert stored["project_root"] == "D:/project"
        assert stored["source_id"] == "src_5"

    def test_only_string_and_integer_values_are_stored(self) -> None:
        stored = self.make_stored()

        assert stored
        for key, value in stored.items():
            assert isinstance(value, str | int), key
            assert not isinstance(value, bool), key

    def test_absent_scoping_values_are_omitted_not_nulled(self) -> None:
        stored = self.make_stored()

        assert "project_root" not in stored
        assert "source_id" not in stored
        assert None not in stored.values()


class TestFrozenCoordinateSpace:
    def test_version_constants_are_frozen(self) -> None:
        assert PARSER_VERSION == "parser-v1"
        assert CHUNKER_VERSION == "page-chunker-v1"
        assert INDEX_VERSION == "index-v1"

    def test_chunk_target_and_overlap_are_frozen(self) -> None:
        assert CHUNK_TARGET_CHARS == 1400
        assert CHUNK_OVERLAP_CHARS == 180
        assert CHUNK_OVERLAP_CHARS < CHUNK_TARGET_CHARS

    def test_embedding_identity_defaults_are_frozen(self) -> None:
        assert DEFAULT_EMBEDDING_MODEL == "chromadb-default"
        assert DEFAULT_EMBEDDING_VERSION == "unpinned"

    def test_evidence_metadata_keys_are_frozen(self) -> None:
        assert EVIDENCE_METADATA_KEYS == (
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

    def test_evidence_error_codes_are_frozen(self) -> None:
        assert {code.name: code.value for code in EvidenceErrorCode} == {
            "MISSING_PAGE_METADATA": "missing_page_metadata",
            "ARTIFACT_HASH_MISMATCH": "artifact_hash_mismatch",
            "STALE_INDEX": "stale_index",
            "INVALID_METADATA": "invalid_metadata",
            "UNKNOWN_PAGE": "unknown_page",
            "COORDINATES_OUT_OF_RANGE": "coordinates_out_of_range",
            "CHUNK_ID_MISMATCH": "chunk_id_mismatch",
            "QUOTE_MISMATCH": "quote_mismatch",
            "EMPTY_QUOTE": "empty_quote",
        }
        assert isinstance(EvidenceErrorCode.UNKNOWN_PAGE, str)

    def test_stored_chunk_identity_payload_is_frozen(self) -> None:
        # sha256("sha256:2:10:20:page-chunker-v1"), first 24 hex characters.
        assert chunk_id_for(ARTIFACT_SHA256, 2, 10, 20) == "chunk_fdb766bf305e3b9bf7ef3d6e"
        assert chunk_id_for(ARTIFACT_SHA256, 3, 10, 20) == "chunk_924fde7884a54bff88f8cd53"

    def test_stored_index_fingerprint_payload_is_frozen(self) -> None:
        # sha256("sha256|parser-v1|page-chunker-v1|chromadb-default|unpinned|index-v1").
        assert index_fingerprint(artifact_sha256=ARTIFACT_SHA256) == "ffefe493e4277072bb4f5fb8"

    def test_slice_boundaries_for_a_fixed_page_are_frozen(self) -> None:
        # Bump CHUNKER_VERSION before changing these persisted coordinates.
        assert page_slices(LONG_PAGE_TEXT) == [(0, 1360), (1180, 2516), (2336, 3059)]


class TestSha256File:
    def test_matches_hashlib_for_a_file(self, tmp_path: Path) -> None:
        payload = b"page-aware evidence"
        target = tmp_path / "artifact.pdf"
        target.write_bytes(payload)

        assert sha256_file(target) == hashlib.sha256(payload).hexdigest()
        assert sha256_file(str(target)) == sha256_file(target)
        assert len(sha256_file(target)) == 64

    def test_hashes_an_empty_file(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.bin"
        target.write_bytes(b"")

        assert sha256_file(target) == hashlib.sha256(b"").hexdigest()
        assert sha256_file(target) == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_hashes_files_larger_than_one_read_block(self, tmp_path: Path) -> None:
        payload = bytes(range(256)) * 10_000
        target = tmp_path / "large.pdf"
        target.write_bytes(payload)

        assert len(payload) > 1024 * 1024
        assert sha256_file(target) == hashlib.sha256(payload).hexdigest()

    def test_hashes_binary_content_without_decoding_it(self, tmp_path: Path) -> None:
        payload = b"\xff\xfe\x00\x80\x9c binary artifact bytes"
        target = tmp_path / "binary.bin"
        target.write_bytes(payload)

        assert sha256_file(target) == hashlib.sha256(payload).hexdigest()

    def test_missing_path_raises_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.pdf"

        with pytest.raises(FileNotFoundError) as excinfo:
            sha256_file(missing)

        assert Path(str(excinfo.value.filename)) == missing
