"""Fixed offline demo corpus for P4 (plan 5.10, decisions D-033/D-034).

The corpus is a *demo and regression* asset, never an evaluation corpus:

* every identifier carries a ``demo-`` prefix so it cannot be mistaken for a real
  arXiv record or DOI;
* the records deliberately declare no open full-text location, so the demo walks
  the documented fallback (acquisition fails with ``access_unavailable``, then the
  local corpus PDF is attached) instead of pretending to download something;
* the provider built from it always reports ``result_mode=fixture``.

A missing or corrupt corpus fails with an explicit :class:`DemoCorpusError`; the
application turns that into "no cached provider", never into a silent fallback.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.literature.models import (
    AccessLocation,
    ExternalIdentifiers,
    PaperRecord,
)
from src.literature.providers.fixture import FixtureProvider

CORPUS_VERSION = 1
CORPUS_KIND = "synthetic-demo-corpus"
DEFAULT_CORPUS_NAME = "literature_demo_corpus.json"
DEMO_PROVENANCE_LABEL = "demo-corpus-v1"


class DemoCorpusError(RuntimeError):
    """The fixed demo corpus is missing, unreadable or structurally wrong."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class DemoCorpus:
    """One loaded demo corpus with page text kept next to the metadata."""

    corpus_id: str
    kind: str
    provenance: str
    confirmed_query: str
    question: str
    records: tuple[PaperRecord, ...]
    search_results: Mapping[str, tuple[str, ...]]
    pages: Mapping[str, tuple[tuple[int, str], ...]]
    index_paper_ids: tuple[str, ...]
    access_locations: Mapping[str, tuple[AccessLocation, ...]]

    def pages_for(self, paper_id: str) -> tuple[tuple[int, str], ...]:
        return self.pages.get(paper_id, ())

    def record_for_provider_id(self, provider_record_id: str) -> PaperRecord | None:
        for record in self.records:
            if record.provider_record_id == provider_record_id:
                return record
        return None

    def records_to_index(self) -> tuple[PaperRecord, ...]:
        wanted = set(self.index_paper_ids)
        return tuple(record for record in self.records if record.paper_id in wanted)


def resolve_corpus_path(configured: str | Path | None = None) -> Path:
    """Locate the corpus the same way ``providers.yaml`` is located."""

    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "config" / DEFAULT_CORPUS_NAME
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parents[3] / "config" / DEFAULT_CORPUS_NAME


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DemoCorpusError("corpus_invalid", f"{field} must be an object")
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DemoCorpusError("corpus_invalid", f"{field} must be a non-empty string")
    return value.strip()


def load_demo_corpus(path: str | Path | None = None) -> DemoCorpus:
    """Load and validate the fixed corpus, refusing anything unexpected."""

    corpus_path = resolve_corpus_path(path)
    try:
        raw = corpus_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DemoCorpusError(
            "corpus_missing",
            "固定演示语料不存在",
            details={"path": str(corpus_path)},
        ) from exc
    except OSError as exc:
        raise DemoCorpusError(
            "corpus_invalid",
            "固定演示语料无法读取",
            details={"path": str(corpus_path), "exception_type": type(exc).__name__},
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DemoCorpusError(
            "corpus_invalid",
            "固定演示语料不是合法 JSON",
            details={"path": str(corpus_path)},
        ) from exc

    root = _require_mapping(payload, "corpus")
    if root.get("version") != CORPUS_VERSION:
        raise DemoCorpusError(
            "corpus_invalid",
            "固定演示语料版本不受支持",
            details={"version": root.get("version")},
        )
    if root.get("kind") != CORPUS_KIND:
        raise DemoCorpusError(
            "corpus_invalid",
            "固定演示语料类型不符，必须显式标记为合成演示语料",
            details={"kind": root.get("kind")},
        )

    corpus_id = _require_text(root.get("corpus_id"), "corpus_id")
    provenance = _require_text(root.get("provenance"), "provenance")
    demo = _require_mapping(root.get("demo"), "demo")
    confirmed_query = _require_text(demo.get("confirmed_query"), "demo.confirmed_query")
    question = _require_text(demo.get("question"), "demo.question")

    raw_records = root.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise DemoCorpusError("corpus_invalid", "records must be a non-empty list")

    records: list[PaperRecord] = []
    pages: dict[str, tuple[tuple[int, str], ...]] = {}
    access: dict[str, tuple[AccessLocation, ...]] = {}
    seen_provider_ids: set[str] = set()
    for index, raw_record in enumerate(raw_records):
        entry = _require_mapping(raw_record, f"records[{index}]")
        provider_record_id = _require_text(
            entry.get("provider_record_id"), f"records[{index}].provider_record_id"
        )
        if provider_record_id in seen_provider_ids:
            raise DemoCorpusError(
                "corpus_invalid",
                "固定演示语料存在重复记录标识",
                details={"provider_record_id": provider_record_id},
            )
        seen_provider_ids.add(provider_record_id)

        raw_pages = entry.get("pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raise DemoCorpusError(
                "corpus_invalid",
                "每条记录都必须带有逐页文本",
                details={"provider_record_id": provider_record_id},
            )
        page_pairs: list[tuple[int, str]] = []
        for page_index, page_text in enumerate(raw_pages, start=1):
            if not isinstance(page_text, str) or not page_text.strip():
                raise DemoCorpusError(
                    "corpus_invalid",
                    "逐页文本不能为空",
                    details={"provider_record_id": provider_record_id, "page": page_index},
                )
            page_pairs.append((page_index, page_text.strip()))

        record = PaperRecord(
            provider="fixture",
            provider_record_id=provider_record_id,
            external_ids=ExternalIdentifiers(),
            title=_require_text(entry.get("title"), f"records[{index}].title"),
            authors=[_require_text(author, "author") for author in entry.get("authors") or []],
            year=int(entry["year"]) if entry.get("year") is not None else None,
            venue=entry.get("venue"),
            abstract=str(entry.get("abstract") or ""),
            categories=[str(item) for item in entry.get("categories") or []],
            record_url=f"https://demo.invalid/records/{provider_record_id}",
            access_locations=[],
            source_query=confirmed_query,
            retrieved_at=datetime(2026, 9, 29, tzinfo=UTC),
        )
        records.append(record)
        pages[record.paper_id] = tuple(page_pairs)
        access[record.paper_id] = ()

    raw_search = _require_mapping(root.get("search_results"), "search_results")
    search_results: dict[str, tuple[str, ...]] = {}
    by_provider_id = {record.provider_record_id: record for record in records}
    for raw_query, references in raw_search.items():
        if not isinstance(references, list):
            raise DemoCorpusError(
                "corpus_invalid", "search_results values must be lists of record ids"
            )
        resolved: list[str] = []
        for reference in references:
            record = by_provider_id.get(str(reference))
            if record is None:
                raise DemoCorpusError(
                    "corpus_invalid",
                    "检索结果引用了不存在的记录",
                    details={"query": str(raw_query), "reference": str(reference)},
                )
            resolved.append(record.paper_id)
        search_results[str(raw_query)] = tuple(resolved)

    if confirmed_query.casefold() not in {query.casefold() for query in search_results}:
        raise DemoCorpusError(
            "corpus_invalid",
            "演示检索式没有对应的固定检索结果",
            details={"confirmed_query": confirmed_query},
        )

    raw_index = demo.get("index_records") or []
    if not isinstance(raw_index, list):
        raise DemoCorpusError("corpus_invalid", "demo.index_records must be a list")
    index_paper_ids: list[str] = []
    for reference in raw_index:
        record = by_provider_id.get(str(reference))
        if record is None:
            raise DemoCorpusError(
                "corpus_invalid",
                "index_records 引用了不存在的记录",
                details={"reference": str(reference)},
            )
        index_paper_ids.append(record.paper_id)

    return DemoCorpus(
        corpus_id=corpus_id,
        kind=CORPUS_KIND,
        provenance=provenance,
        confirmed_query=confirmed_query,
        question=question,
        records=tuple(records),
        search_results=search_results,
        pages=pages,
        index_paper_ids=tuple(index_paper_ids),
        access_locations=access,
    )


def build_fixture_provider(corpus: DemoCorpus) -> FixtureProvider:
    """Build the offline provider the demo corpus declares."""

    return FixtureProvider(
        fixture_name=corpus.corpus_id,
        snapshot_at=datetime(2026, 9, 29, tzinfo=UTC),
        records=corpus.records,
        search_results=corpus.search_results,
        access_locations=corpus.access_locations,
    )


def _wrap_lines(text: str, width: int = 92) -> list[str]:
    """Wrap page text on spaces so the rendered PDF stays on the page."""

    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def materialize_pdf(corpus: DemoCorpus, paper_id: str, target: str | Path) -> Path:
    """Render one corpus record's page text into a real, parseable PDF."""

    pages = corpus.pages_for(paper_id)
    if not pages:
        raise DemoCorpusError(
            "unknown_paper",
            "固定演示语料中没有这篇文献的逐页文本",
            details={"paper_id": paper_id},
        )
    import fitz

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    try:
        for _, text in pages:
            page = document.new_page()
            for index, line in enumerate(_wrap_lines(text)):
                if not line:
                    continue
                page.insert_text((72, 96 + index * 16), line, fontsize=11)
        document.save(str(destination))
    finally:
        document.close()
    return destination


__all__ = [
    "CORPUS_KIND",
    "CORPUS_VERSION",
    "DEFAULT_CORPUS_NAME",
    "DemoCorpus",
    "DemoCorpusError",
    "build_fixture_provider",
    "load_demo_corpus",
    "materialize_pdf",
    "resolve_corpus_path",
]
