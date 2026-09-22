"""Behavioral contract tests for literature providers and the offline fixture."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.literature import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    FixtureProvider,
    LiteratureProvider,
    LiteratureProviderError,
    PaperRecord,
    ProviderErrorCode,
    ProviderOperation,
    SearchFilters,
    SearchQuery,
    SearchResultMode,
    SearchSortBy,
    SearchSortOrder,
)
from tests.unit.provider_contract_helpers import assert_provider_contract

SNAPSHOT_AT = datetime(2026, 9, 20, 1, 2, 3, tzinfo=UTC)
QUERY_TEXT = "multi agent academic writing"


def make_paper(
    index: int,
    *,
    year: int | None,
    categories: list[str],
    with_access: bool = True,
) -> PaperRecord:
    arxiv_id = f"2401.1000{index}"
    access_locations = []
    if with_access:
        access_locations.append(
            AccessLocation(
                kind=AccessKind.PDF,
                url=f"https://arxiv.org/pdf/{arxiv_id}",
                access_status=AccessStatus.OPEN,
                mime_type="application/pdf",
                is_primary=True,
            )
        )
    return PaperRecord(
        provider="arxiv",
        provider_record_id=arxiv_id,
        external_ids={
            "doi": f"10.1000/fixture-{index}",
            "arxiv": f"{arxiv_id}v1",
        },
        title=f"Fixture Paper {index}",
        authors=[f"Author {index}"],
        year=year,
        venue="arXiv",
        abstract=f"Fixture abstract {index}",
        categories=categories,
        record_url=f"https://arxiv.org/abs/{arxiv_id}",
        access_locations=access_locations,
        source_query=QUERY_TEXT,
        retrieved_at=SNAPSHOT_AT,
    )


def make_provider(
    *,
    operation_errors: dict[ProviderOperation | str, LiteratureProviderError] | None = None,
) -> tuple[FixtureProvider, list[PaperRecord]]:
    records = [
        make_paper(1, year=2022, categories=["cs.AI"]),
        make_paper(2, year=2024, categories=["cs.CL"], with_access=False),
        make_paper(3, year=2023, categories=["cs.AI", "cs.CL"]),
    ]
    provider = FixtureProvider(
        fixture_name="literature-contract-v1",
        snapshot_at=SNAPSHOT_AT,
        records=records,
        search_results={
            QUERY_TEXT: [
                records[0].provider_record_id,
                records[1].external_ids.doi,
                records[2].paper_id,
            ]
        },
        operation_errors=operation_errors,
    )
    return provider, records


class TestFixtureProviderContract:
    async def test_shared_provider_contract(self) -> None:
        provider, records = make_provider()
        await assert_provider_contract(provider, records, query_text=QUERY_TEXT)

    async def test_fixture_results_are_never_marked_live(self) -> None:
        provider, _ = make_provider()
        page = await provider.search(SearchQuery(query=QUERY_TEXT))

        assert provider.capabilities().result_mode is SearchResultMode.FIXTURE
        assert page.result_mode is SearchResultMode.FIXTURE
        assert page.provenance_label == "literature-contract-v1"

    async def test_pagination_is_deterministic(self) -> None:
        provider, records = make_provider()
        second_page = await provider.search(SearchQuery(query=QUERY_TEXT, page=2, page_size=2))

        assert [item.paper_id for item in second_page.records] == [records[2].paper_id]
        assert second_page.total_results == 3
        assert second_page.has_more is False

    async def test_zero_results_is_valid_not_an_error(self) -> None:
        provider, _ = make_provider()
        page = await provider.search(SearchQuery(query="not configured"))

        assert page.records == []
        assert page.total_results == 0
        assert page.result_mode is SearchResultMode.FIXTURE

    async def test_lookup_accepts_normalized_external_identifiers(self) -> None:
        provider, records = make_provider()

        by_doi = await provider.get_record("HTTPS://DOI.ORG/10.1000/FIXTURE-1")
        by_arxiv_version = await provider.get_record("2401.10001v1")

        assert by_doi.paper_id == records[0].paper_id
        assert by_arxiv_version.paper_id == records[0].paper_id

    async def test_lookup_accepts_long_doi_without_leaking_validation_error(self) -> None:
        long_doi = "10.1000/" + "x" * 140
        record = make_paper(9, year=2024, categories=["cs.AI"])
        payload = record.model_dump(mode="json")
        payload["external_ids"]["doi"] = long_doi
        payload["metadata_snapshot_hash"] = ""
        record = PaperRecord.model_validate(payload)
        provider = FixtureProvider(
            fixture_name="long-doi-v1",
            snapshot_at=SNAPSHOT_AT,
            records=[record],
            search_results={},
        )

        fetched = await provider.get_record(f"https://doi.org/{long_doi}")
        assert fetched.paper_id == record.paper_id

    async def test_unknown_record_is_explicit_not_found(self) -> None:
        provider, _ = make_provider()

        with pytest.raises(LiteratureProviderError) as exc_info:
            await provider.get_record("missing-id")

        error = exc_info.value
        assert error.code is ProviderErrorCode.NOT_FOUND
        assert error.operation is ProviderOperation.GET_RECORD
        assert error.status_code == 404
        assert error.retryable is False

    async def test_known_record_without_access_returns_empty_list(self) -> None:
        provider, records = make_provider()
        record = await provider.get_record(records[1].paper_id)

        assert await provider.resolve_access(record) == []

    async def test_filters_and_sorting_are_applied_after_fixed_snapshot(self) -> None:
        provider, records = make_provider()
        page = await provider.search(
            SearchQuery(
                query=QUERY_TEXT,
                page_size=10,
                sort_by=SearchSortBy.YEAR,
                sort_order=SearchSortOrder.DESCENDING,
                filters=SearchFilters(year_from=2023, categories=["cs.CL"]),
            )
        )

        assert [item.paper_id for item in page.records] == [
            records[1].paper_id,
            records[2].paper_id,
        ]

    async def test_category_filters_use_documented_or_semantics(self) -> None:
        provider, records = make_provider()
        page = await provider.search(
            SearchQuery(
                query=QUERY_TEXT,
                filters=SearchFilters(categories=["cs.AI", "unmatched.category"]),
            )
        )

        assert [item.paper_id for item in page.records] == [
            records[0].paper_id,
            records[2].paper_id,
        ]

    async def test_unknown_year_sorts_after_known_years_in_both_directions(self) -> None:
        records = [
            make_paper(7, year=None, categories=["cs.AI"]),
            make_paper(8, year=2024, categories=["cs.AI"]),
            make_paper(9, year=2022, categories=["cs.AI"]),
        ]
        provider = FixtureProvider(
            fixture_name="year-sort-v1",
            snapshot_at=SNAPSHOT_AT,
            records=records,
            search_results={QUERY_TEXT: [record.paper_id for record in records]},
        )

        ascending = await provider.search(
            SearchQuery(
                query=QUERY_TEXT,
                sort_by=SearchSortBy.YEAR,
                sort_order=SearchSortOrder.ASCENDING,
            )
        )
        descending = await provider.search(
            SearchQuery(
                query=QUERY_TEXT,
                sort_by=SearchSortBy.YEAR,
                sort_order=SearchSortOrder.DESCENDING,
            )
        )

        assert [record.year for record in ascending.records] == [2022, 2024, None]
        assert [record.year for record in descending.records] == [2024, 2022, None]

    async def test_relevance_keeps_curated_order_in_both_directions(self) -> None:
        provider, records = make_provider()
        curated = [record.paper_id for record in records]

        descending = await provider.search(
            SearchQuery(query=QUERY_TEXT, sort_by=SearchSortBy.RELEVANCE)
        )
        ascending = await provider.search(
            SearchQuery(
                query=QUERY_TEXT,
                sort_by=SearchSortBy.RELEVANCE,
                sort_order=SearchSortOrder.ASCENDING,
            )
        )

        assert [item.paper_id for item in descending.records] == curated
        assert [item.paper_id for item in ascending.records] == curated
        assert len(curated) > 1

    async def test_version_specific_lookup_cannot_resolve_to_another_version(self) -> None:
        provider, records = make_provider()
        versioned = next(
            record for record in records if record.external_ids.arxiv_version is not None
        )
        requested_version = versioned.external_ids.arxiv_version
        arxiv_id = versioned.external_ids.arxiv

        matching = await provider.get_record(f"{arxiv_id}v{requested_version}")
        assert matching.paper_id == versioned.paper_id
        assert (await provider.get_record(arxiv_id)).paper_id == versioned.paper_id

        with pytest.raises(LiteratureProviderError) as exc_info:
            await provider.get_record(f"{arxiv_id}v{requested_version + 1}")

        assert exc_info.value.code is ProviderErrorCode.NOT_FOUND

    async def test_returned_objects_do_not_mutate_fixture_state(self) -> None:
        provider, records = make_provider()
        first = await provider.search(SearchQuery(query=QUERY_TEXT))

        with pytest.raises(TypeError, match="immutable"):
            first.records[0].authors.append("Mutated Caller State")
        with pytest.raises(TypeError, match="immutable"):
            first.records[0].access_locations.clear()
        with pytest.raises(ValidationError, match="frozen"):
            first.records[0].title = "Mutated Caller State"

        second = await provider.search(SearchQuery(query=QUERY_TEXT))
        assert second.records[0].authors == records[0].authors
        assert second.records[0].access_locations == records[0].access_locations


class TestFixtureConfiguration:
    def test_dangling_search_reference_fails_at_construction(self) -> None:
        record = make_paper(1, year=2024, categories=["cs.AI"])

        with pytest.raises(ValueError, match="does not resolve"):
            FixtureProvider(
                fixture_name="bad-fixture",
                snapshot_at=SNAPSHOT_AT,
                records=[record],
                search_results={QUERY_TEXT: ["missing-id"]},
            )

    def test_duplicate_identity_fails_at_construction(self) -> None:
        first = make_paper(1, year=2024, categories=["cs.AI"])
        duplicate = first.model_copy(deep=True)

        with pytest.raises(ValueError, match="duplicate paper_id"):
            FixtureProvider(
                fixture_name="bad-fixture",
                snapshot_at=SNAPSHOT_AT,
                records=[first, duplicate],
                search_results={},
            )

    def test_fixture_requires_aware_snapshot_time(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            FixtureProvider(
                fixture_name="bad-time",
                snapshot_at=datetime(2026, 9, 20),
                records=[],
                search_results={},
            )

    def test_fixture_revalidates_bypassed_model_copy(self) -> None:
        valid = make_paper(1, year=2024, categories=["cs.AI"])
        invalid = valid.model_copy(update={"title": ""})

        with pytest.raises(ValidationError):
            FixtureProvider(
                fixture_name="invalid-record",
                snapshot_at=SNAPSHOT_AT,
                records=[invalid],
                search_results={},
            )


class TestProviderFailures:
    @pytest.mark.parametrize(
        ("code", "status_code", "retryable"),
        [
            (ProviderErrorCode.RATE_LIMITED, 429, True),
            (ProviderErrorCode.UNAVAILABLE, 503, True),
            (ProviderErrorCode.INVALID_RESPONSE, 502, False),
        ],
    )
    async def test_search_failure_is_typed_and_not_an_empty_page(
        self,
        code: ProviderErrorCode,
        status_code: int,
        retryable: bool,
    ) -> None:
        injected = LiteratureProviderError(
            code,
            f"injected {code.value}",
            provider="fixture",
            operation=ProviderOperation.SEARCH,
            retry_after_seconds=2.0 if code is ProviderErrorCode.RATE_LIMITED else None,
        )
        provider, _ = make_provider(operation_errors={ProviderOperation.SEARCH: injected})

        with pytest.raises(LiteratureProviderError) as exc_info:
            await provider.search(SearchQuery(query=QUERY_TEXT))

        error = exc_info.value
        assert error is not injected
        assert error.code is code
        assert error.operation is ProviderOperation.SEARCH
        assert error.status_code == status_code
        assert error.retryable is retryable
        assert error.to_dict()["code"] == code.value

    async def test_nested_error_details_are_isolated_between_attempts(self) -> None:
        injected = LiteratureProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "injected unavailable",
            provider="fixture",
            operation=ProviderOperation.SEARCH,
            details={"attempt": {"number": 1}},
        )
        provider, _ = make_provider(operation_errors={ProviderOperation.SEARCH: injected})

        with pytest.raises(LiteratureProviderError) as first_info:
            await provider.search(SearchQuery(query=QUERY_TEXT))
        first_info.value.details["attempt"]["number"] = 99

        with pytest.raises(LiteratureProviderError) as second_info:
            await provider.search(SearchQuery(query=QUERY_TEXT))

        assert injected.details["attempt"]["number"] == 1
        assert second_info.value.details["attempt"]["number"] == 1
