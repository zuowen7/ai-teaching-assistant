"""Article boundary detector — split multi-article PDFs before the cleaner runs.

Science Perspectives, Nature News & Views, and similar journal formats often
bundle 3-5 short articles in a single PDF.  The cleaner strips article boundary
markers (watermarks, footers), so detection must happen on raw extracted text
before cleaning.

Two detection strategies:
  A) Title + author + journal info pattern (when visible at article start)
  B) Content heuristics: truncated paragraph starts signal new articles
  C) Stranded initial: the extractor puts the opening word's first letter on its
     own line ("I" then "n 2023, extreme heat ..."), which leaves no blank line
     for strategy B to key on
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Author pattern: "First M. Last" / "First Last and First M. Last"
_AUTHOR_RE = re.compile(
    r"^[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+"
    r"(?:\s+(?:and|&|,)\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)*\s*$",
    re.MULTILINE,
)

_JOURNAL_RE = re.compile(
    r"^(?:Science|Nature|Cell|PNAS|Lancet)\s+\d+",
    re.MULTILINE,
)

_TITLE_RE = re.compile(
    r"^[A-Z][a-zA-Z\s:,\-]{9,95}$",
    re.MULTILINE,
)

# Truncated word fixes for boundary recovery
_TRUNCATION_FIXES = {
    "n": "In",
    "t": "At",
    "s": "As",
    "b": "By",
    "o": "Of",
    "f": "For",
    "w": "With",
    "a": "A",
}

_TRUNCATED_WORD_FIXES = {
    "nflammation": "Inflammation",
    "nvironmental": "Environmental",
    "pigenetic": "Epigenetic",
    "vidence": "Evidence",
    "volutionary": "Evolutionary",
    "stimated": "Estimated",
}


def detect_articles(raw_text: str) -> list[tuple[int, int, str]]:
    """Detect article boundaries in raw extracted text (before cleaning).

    Returns:
        [(start_pos, end_pos, title), ...] — sorted by start_pos.
        If ≤1 article found, returns a single entry spanning the whole text.
    """
    # Strategy C: a stranded opening letter is the most precise signal.
    boundaries_c = _detect_by_split_initial(raw_text)
    if len(boundaries_c) > 1:
        return boundaries_c

    # Strategy A: title + author + journal info
    boundaries_a = _detect_by_title_author(raw_text)
    if len(boundaries_a) > 1:
        return boundaries_a

    # Strategy B: truncated paragraph starts as boundary signals
    boundaries_b = _detect_by_truncation(raw_text)
    if len(boundaries_b) > 1:
        return boundaries_b

    return [(0, len(raw_text), "")]


def extract_articles(raw_text: str) -> list[str]:
    """Split raw text into independent article texts.

    Returns [raw_text] if no article boundaries detected.
    """
    boundaries = detect_articles(raw_text)
    if len(boundaries) <= 1:
        return [raw_text]

    articles = []
    for start, end, _title in boundaries:
        articles.append(_repair_split_initial(raw_text[start:end]))

    logger.info(
        "Split into %d articles: %s",
        len(articles),
        [t[:50] if t else f"(article {i + 1})" for i, (_, _, t) in enumerate(boundaries)],
    )
    return articles


# ---------------------------------------------------------------------------
# Strategy C: stranded opening letter
# ---------------------------------------------------------------------------

_LONE_LETTER_RE = re.compile(r"^[A-Za-z]$")


def _repaired_word(fragment: str) -> str | None:
    """Return the full word for a known truncated fragment, else None."""

    key = fragment.lower().rstrip(".,;:!?")
    return _TRUNCATION_FIXES.get(key) or _TRUNCATED_WORD_FIXES.get(key)


def _repair_split_initial(text: str) -> str:
    """Rejoin a first letter the extractor stranded on its own line."""

    if re.match(r"^[A-Za-z]\n[a-z]", text) is None:
        return text
    return text[0] + text[2:]


def _detect_by_split_initial(raw_text: str) -> list[tuple[int, int, str]]:
    """Detect boundaries where the opening word lost its first letter.

    The extractor sometimes emits::

        Laurie S. Huning and Manuela I. Brunner
        I
        n 2023, extreme heat propelled the spread of severe

    so the truncated start is not preceded by a blank line and strategy B cannot
    see it.  The lone letter must be the missing capital of a *known* truncated
    word, which keeps the rule free of ordinary "I" pronoun lines.
    """

    boundaries: list[tuple[int, int, str]] = []

    lines = raw_text.split("\n")
    line_offsets: list[int] = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line) + 1

    for i in range(1, len(lines)):
        letter = lines[i - 1].strip()
        if not _LONE_LETTER_RE.match(letter):
            continue
        candidate = lines[i].strip()
        if not candidate:
            continue
        fragment = candidate.split()[0].rstrip(".,;:!?")
        repaired = _repaired_word(fragment)
        if repaired is None:
            continue
        if (letter + fragment).lower() != repaired.lower():
            continue
        boundaries.append((line_offsets[i - 1], -1, repaired))

    if not boundaries:
        return [(0, len(raw_text), "")]

    if boundaries[0][0] > 0:
        boundaries.insert(0, (0, boundaries[0][0], ""))

    for idx in range(len(boundaries) - 1):
        s, _, t = boundaries[idx]
        boundaries[idx] = (s, boundaries[idx + 1][0], t)
    s, _, t = boundaries[-1]
    boundaries[-1] = (s, len(raw_text), t)
    return boundaries


# ---------------------------------------------------------------------------
# Strategy A: title + author + journal pattern
# ---------------------------------------------------------------------------


def _detect_by_title_author(raw_text: str) -> list[tuple[int, int, str]]:
    boundaries: list[tuple[int, int, str]] = []

    lines = raw_text.split("\n")
    line_offsets: list[int] = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line) + 1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) < 10 or len(stripped) > 100:
            continue
        if not _TITLE_RE.match(stripped):
            continue

        # Title-like validation: no period/question/excl, multiple words, not too short
        if stripped[-1] in ".!?":
            continue
        words = stripped.split()
        if len(words) < 3:
            continue
        # At least half the words should start with uppercase (Title Case or ALL CAPS)
        upper_count = sum(1 for w in words if w[0].isupper())
        if upper_count / len(words) < 0.4:
            continue

        found_author = False
        found_journal = False
        for j in range(i + 1, min(i + 13, len(lines))):
            j_stripped = lines[j].strip()
            if not j_stripped:
                continue
            if not found_author and _AUTHOR_RE.match(j_stripped):
                found_author = True
            elif found_author and _JOURNAL_RE.match(j_stripped):
                found_journal = True
                break

        if found_author and found_journal:
            boundaries.append((line_offsets[i], -1, stripped))

    if not boundaries:
        return [(0, len(raw_text), "")]

    # If markers found, the first article starts at position 0
    if boundaries[0][0] > 0:
        boundaries.insert(0, (0, boundaries[0][0], ""))

    for idx in range(len(boundaries) - 1):
        s, _, t = boundaries[idx]
        boundaries[idx] = (s, boundaries[idx + 1][0], t)
    s, _, t = boundaries[-1]
    boundaries[-1] = (s, len(raw_text), t)
    return boundaries


# ---------------------------------------------------------------------------
# Strategy B: truncated paragraph starts
# ---------------------------------------------------------------------------

# Pattern: blank line(s) followed by a truncated paragraph start
# Truncated starts: single lowercase letter + space + digit/uppercase word,
# or a known truncated word like "nflammation"
_TRUNCATED_START_RE = re.compile(
    r"\n\n"
    r"(?:[^\S\n]*\n)*"  # optional noise lines between articles
    r"([a-z]\s+[A-Z0-9])"  # "n 2023", "s As", etc.
    r"|"
    r"\n\n"
    r"([a-z]{2,}[a-z])\s"  # "nflammation ", "pigenetic "
)

# Known noise patterns between articles (PDF page boundary artifacts)
_NOISE_LINE_RE = re.compile(
    r"^[óñáéíúäëïöüåæœ\W\s]*$",
)


def _detect_by_truncation(raw_text: str) -> list[tuple[int, int, str]]:
    """Detect article boundaries by finding truncated paragraph starts.

    Signal: an empty-line gap followed by a paragraph that starts with a
    truncated word (lowercase first char that should be uppercase).
    This happens when multi-article PDFs are extracted and the first letter
    of each new article gets cut off at a page boundary.
    """
    boundaries: list[tuple[int, int, str]] = [(0, len(raw_text), "")]

    lines = raw_text.split("\n")
    line_offsets: list[int] = []
    pos = 0
    for line in lines:
        line_offsets.append(pos)
        pos += len(line) + 1

    # Scan for: empty line(s) → possibly noise lines → truncated start
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip until we find an empty line
        if line:
            i += 1
            continue

        # We're at an empty line — look ahead for truncated start
        j = i + 1
        # Skip noise lines (ó ó, blank, etc.)
        while j < len(lines) and _NOISE_LINE_RE.match(lines[j].strip()):
            j += 1

        if j >= len(lines):
            i += 1
            continue

        candidate = lines[j].strip()
        if not candidate:
            i += 1
            continue

        # Check if this line starts with a truncated word
        boundary_offset = _check_truncated_start(candidate)
        if boundary_offset is not None:
            # This looks like an article boundary
            article_start = line_offsets[j]
            boundaries.append((article_start, len(raw_text), ""))
            i = j + 1
        else:
            i += 1

    if len(boundaries) <= 1:
        return [(0, len(raw_text), "")]

    # Fill end positions
    for idx in range(len(boundaries) - 1):
        s, _, t = boundaries[idx]
        boundaries[idx] = (s, boundaries[idx + 1][0], t)
    s, _, t = boundaries[-1]
    boundaries[-1] = (s, len(raw_text), t)

    return boundaries


def _check_truncated_start(line: str) -> int | None:
    """Check if a line starts with a truncated word. Returns confidence or None."""
    words = line.split()
    if not words:
        return None

    first = words[0]

    # Pattern 1: single lowercase letter + space + digit/uppercase word
    # e.g., "n 2023", "s As"
    if (
        len(first) == 1
        and first.islower()
        and len(words) >= 2
        and (words[1][0].isdigit() or words[1][0].isupper())
    ):
        return 1

    # Pattern 2: known truncated words
    first_lower = first.lower().rstrip(".,;:!?")
    if first_lower in _TRUNCATED_WORD_FIXES:
        return 1

    # Pattern 3: word starts with lowercase but looks like it should be capitalized
    # (3+ lowercase chars that are a known prefix of a common word)
    if len(first) >= 5 and first[0].islower() and first.isalpha():
        # Check if capitalizing the first letter gives a common English word
        first[0].upper() + first[1:]
        # Common academic words that get truncated
        _TRUNCATED_PREFIXES = {
            "nflam": "Inflam",
            "pigen": "Epigen",
            "nviron": "Envir",
            "volve": "Evol",
            "stima": "Estima",
            "ffect": "Effect",
        }
        prefix = first[:5].lower()
        if prefix in _TRUNCATED_PREFIXES:
            return 1

    return None
