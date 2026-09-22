"""Deterministic, offline literature provider for contracts and demos."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from src.literature.models import (
    AccessLocation,
    ExternalIdentifiers,
    PaperRecord,
    ProviderCapabilities,
    SearchPage,
    SearchQuery,
    SearchResultMode,
    SearchSortBy,
    SearchSortOrder,
)
from src.literature.providers.base import (
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)


def _query_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _lookup_key(value: str) -> str:
    return value.strip().casefold()


def _lookup_candidates(value: str) -> list[str]:
    """Return raw and normalized DOI/arXiv lookup keys for one identifier."""

    candidates = [_lookup_key(value)]
    try:
        doi = ExternalIdentifiers(doi=value).doi
        if doi:
            candidates.append(_lookup_key(doi))
    except ValueError:
        pass
    try:
        arxiv = ExternalIdentifiers(arxiv=value)
        candidates.extend(_lookup_key(item) for item in arxiv.lookup_values())
    except ValueError:
        pass
    return list(dict.fromkeys(candidates))


def _validated_record(record: PaperRecord) -> PaperRecord:
    return PaperRecord.model_validate(record.model_dump(mode="json"))


def _matches_requested_version(record: PaperRecord, external_id: str) -> bool:
    """Reject a version-specific lookup that would resolve to another version.

    Lookup keys are version-free, so without this check a request for
    ``2401.10001v1`` would happily return the configured ``v2`` record while
    claiming a different version was found.
    """

    try:
        requested_version = ExternalIdentifiers(arxiv=external_id).arxiv_version
    except ValueError:
        return True
    if requested_version is None or record.external_ids.arxiv is None:
        return True
    return record.external_ids.arxiv_version == requested_version


def _validated_location(location: AccessLocation) -> AccessLocation:
    return AccessLocation.model_validate(location.model_dump(mode="json"))


class FixtureProvider:
    """Return constructor-injected snapshots without performing network I/O.

    ``search_results`` maps an exact normalized query to record identifiers in
    fixed relevance order.  This intentionally does not emulate arXiv query
    parsing: fixture behavior must be explicit rather than falsely presented as
    equivalent to a live provider.
    """

    def __init__(
        self,
        *,
        fixture_name: str,
        snapshot_at: datetime,
        records: Iterable[PaperRecord],
        search_results: Mapping[str, Sequence[str]],
        access_locations: Mapping[str, Sequence[AccessLocation]] | None = None,
        operation_errors: Mapping[ProviderOperation | str, LiteratureProviderError] | None = None,
    ) -> None:
        normalized_fixture_name = fixture_name.strip()
        if not normalized_fixture_name:
            raise ValueError("fixture_name cannot be blank")
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must include a timezone")

        self._fixture_name = normalized_fixture_name
        self._snapshot_at = snapshot_at.astimezone(UTC)
        self._records_by_paper_id: dict[str, PaperRecord] = {}
        self._records_by_lookup: dict[str, PaperRecord] = {}

        for raw_record in records:
            record = _validated_record(raw_record)
            if record.paper_id in self._records_by_paper_id:
                raise ValueError(f"duplicate paper_id in fixture: {record.paper_id}")
            self._records_by_paper_id[record.paper_id] = record
            self._register_lookup(record.paper_id, record)
            self._register_lookup(record.provider_record_id, record)
            for external_id in record.external_ids.lookup_values():
                self._register_lookup(external_id, record)

        self._search_results: dict[str, tuple[str, ...]] = {}
        for raw_query, references in search_results.items():
            key = _query_key(raw_query)
            if not key:
                raise ValueError("fixture search query cannot be blank")
            if key in self._search_results:
                raise ValueError(f"duplicate normalized fixture query: {raw_query}")
            paper_ids: list[str] = []
            seen: set[str] = set()
            for reference in references:
                record = self._find_configured_record(reference)
                if record.paper_id in seen:
                    raise ValueError(
                        f"fixture query {raw_query!r} contains duplicate record {reference!r}"
                    )
                seen.add(record.paper_id)
                paper_ids.append(record.paper_id)
            self._search_results[key] = tuple(paper_ids)

        self._access_by_paper_id: dict[str, tuple[AccessLocation, ...]] = {
            paper_id: tuple(_validated_location(location) for location in record.access_locations)
            for paper_id, record in self._records_by_paper_id.items()
        }
        for reference, locations in (access_locations or {}).items():
            record = self._find_configured_record(reference)
            self._access_by_paper_id[record.paper_id] = tuple(
                _validated_location(location) for location in locations
            )

        self._operation_errors: dict[ProviderOperation, LiteratureProviderError] = {}
        for raw_operation, error in (operation_errors or {}).items():
            operation = ProviderOperation(raw_operation)
            if error.provider != self.name:
                raise ValueError("fixture operation error provider must be 'fixture'")
            if error.operation is not operation:
                raise ValueError("fixture operation error is attached to the wrong operation")
            self._operation_errors[operation] = error.clone()

    @property
    def name(self) -> str:
        return "fixture"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            result_mode=SearchResultMode.FIXTURE,
            supports_search=True,
            supports_record_lookup=True,
            supports_access_resolution=True,
            supports_fulltext_download=False,
            supports_pagination=True,
            max_page_size=100,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        self._maybe_raise(ProviderOperation.SEARCH)
        validated_query = SearchQuery.model_validate(query.model_dump(mode="json"))
        paper_ids = list(self._search_results.get(_query_key(validated_query.query), ()))
        records = [self._records_by_paper_id[paper_id] for paper_id in paper_ids]
        records = self._apply_filters(records, validated_query)
        records = self._apply_sort(records, validated_query)

        total_results = len(records)
        start = (validated_query.page - 1) * validated_query.page_size
        end = start + validated_query.page_size
        page_records = [_validated_record(record) for record in records[start:end]]
        return SearchPage(
            provider=self.name,
            result_mode=SearchResultMode.FIXTURE,
            query=validated_query,
            records=page_records,
            total_results=total_results,
            has_more=end < total_results,
            retrieved_at=self._snapshot_at,
            provenance_label=self._fixture_name,
        )

    async def get_record(self, external_id: str) -> PaperRecord:
        self._maybe_raise(ProviderOperation.GET_RECORD)
        record = next(
            (
                self._records_by_lookup[key]
                for key in _lookup_candidates(external_id)
                if key in self._records_by_lookup
            ),
            None,
        )
        if record is not None and not _matches_requested_version(record, external_id):
            record = None
        if record is None:
            raise LiteratureProviderError(
                ProviderErrorCode.NOT_FOUND,
                f"fixture record not found: {external_id}",
                provider=self.name,
                operation=ProviderOperation.GET_RECORD,
            )
        return _validated_record(record)

    async def resolve_access(self, record: PaperRecord) -> list[AccessLocation]:
        self._maybe_raise(ProviderOperation.RESOLVE_ACCESS)
        configured_record = self._records_by_paper_id.get(record.paper_id)
        if configured_record is None:
            raise LiteratureProviderError(
                ProviderErrorCode.NOT_FOUND,
                f"fixture record not found: {record.paper_id}",
                provider=self.name,
                operation=ProviderOperation.RESOLVE_ACCESS,
            )
        return [
            _validated_location(location)
            for location in self._access_by_paper_id[configured_record.paper_id]
        ]

    def _register_lookup(self, value: str, record: PaperRecord) -> None:
        key = _lookup_key(value)
        existing = self._records_by_lookup.get(key)
        if existing is not None and existing.paper_id != record.paper_id:
            raise ValueError(f"duplicate fixture lookup identifier: {value}")
        self._records_by_lookup[key] = record

    def _find_configured_record(self, reference: str) -> PaperRecord:
        record = self._records_by_lookup.get(_lookup_key(reference))
        if record is None:
            raise ValueError(f"fixture reference does not resolve to a record: {reference}")
        return record

    def _maybe_raise(self, operation: ProviderOperation) -> None:
        error = self._operation_errors.get(operation)
        if error is not None:
            raise error.clone()

    @staticmethod
    def _apply_filters(records: list[PaperRecord], query: SearchQuery) -> list[PaperRecord]:
        year_from = query.filters.year_from
        year_to = query.filters.year_to
        categories = {category.casefold() for category in query.filters.categories}
        filtered: list[PaperRecord] = []
        for record in records:
            if year_from is not None and (record.year is None or record.year < year_from):
                continue
            if year_to is not None and (record.year is None or record.year > year_to):
                continue
            if categories:
                record_categories = {category.casefold() for category in record.categories}
                if not categories.intersection(record_categories):
                    continue
            filtered.append(record)
        return filtered

    @staticmethod
    def _apply_sort(records: list[PaperRecord], query: SearchQuery) -> list[PaperRecord]:
        ordered = list(records)
        reverse = query.sort_order is SearchSortOrder.DESCENDING
        if query.sort_by is SearchSortBy.YEAR:
            known_year = [record for record in ordered if record.year is not None]
            unknown_year = [record for record in ordered if record.year is None]
            known_year.sort(key=lambda item: (item.year, item.paper_id), reverse=reverse)
            ordered = known_year + unknown_year
        # RELEVANCE keeps the curated fixture order in both directions: the
        # fixture declares one relevance ranking, and reversing it would invent
        # a ranking that was never configured.
        return ordered


__all__ = ["FixtureProvider"]
