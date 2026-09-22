"""Transport-isolated tests for the structured arXiv provider."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

import src.literature.providers.arxiv as arxiv_module
from src.literature import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    ArxivProvider,
    ArxivRequestGate,
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
QUERY_TEXT = 'all:"multi agent" AND all:"academic writing"'


def atom_entry(index: int, *, version: int | None = None) -> str:
    arxiv_id = f"2401.1000{index}"
    full_id = f"{arxiv_id}v{version}" if version is not None else arxiv_id
    journal_ref = f"<arxiv:journal_ref>Journal {index}</arxiv:journal_ref>" if index != 2 else ""
    return f"""
      <entry>
        <id>http://arxiv.org/abs/{full_id}</id>
        <updated>2024-02-0{index}T12:00:00Z</updated>
        <published>2024-01-0{index}T12:00:00Z</published>
        <title> Fixture   Paper {index} </title>
        <summary> Fixture abstract {index}. </summary>
        <author><name>Author {index}</name></author>
        <author><name>Coauthor {index}</name></author>
        <arxiv:doi>10.1000/fixture-{index}</arxiv:doi>
        {journal_ref}
        <arxiv:primary_category term="cs.AI" />
        <category term="cs.AI" />
        <category term="cs.CL" />
        <link href="http://arxiv.org/abs/{full_id}" rel="alternate" type="text/html" />
        <link href="http://arxiv.org/pdf/{full_id}" rel="related"
              title="pdf" type="application/pdf" />
      </entry>
    """


def legacy_atom_entry(*, version: int = 2) -> str:
    return f"""
      <entry>
        <id>http://arxiv.org/abs/hep-ex/0307015v{version}</id>
        <updated>2003-08-01T12:00:00-04:00</updated>
        <published>2003-07-07T13:46:39-04:00</published>
        <title>Legacy Identifier Paper</title>
        <summary>Legacy identifier fixture.</summary>
        <author><name>H1 Collaboration</name></author>
        <category term="hep-ex" />
      </entry>
    """


def atom_feed(
    entries: list[str],
    *,
    total: int,
    start: int = 0,
    items_per_page: int | None = None,
) -> str:
    item_count = len(entries) if items_per_page is None else items_per_page
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
          xmlns:arxiv="http://arxiv.org/schemas/atom">
      <title>arXiv Query</title>
      <id>https://arxiv.org/api/test-feed</id>
      <updated>2026-09-20T00:00:00Z</updated>
      <opensearch:totalResults>{total}</opensearch:totalResults>
      <opensearch:startIndex>{start}</opensearch:startIndex>
      <opensearch:itemsPerPage>{item_count}</opensearch:itemsPerPage>
      {"".join(entries)}
    </feed>"""


def atom_response(body: str, *, content_type: str = "application/atom+xml") -> httpx.Response:
    return httpx.Response(
        200,
        text=body,
        headers={"Content-Type": content_type},
    )


def expected_record(
    index: int,
    *,
    version: int | None = None,
    source_query: str = QUERY_TEXT,
    access_locations: list[AccessLocation] | None = None,
) -> PaperRecord:
    arxiv_id = f"2401.1000{index}"
    full_id = f"{arxiv_id}v{version}" if version is not None else arxiv_id
    locations = access_locations
    if locations is None:
        locations = [
            AccessLocation(
                kind=AccessKind.PDF,
                url=f"https://arxiv.org/pdf/{full_id}",
                access_status=AccessStatus.OPEN,
                mime_type="application/pdf",
                is_primary=True,
            ),
            AccessLocation(
                kind=AccessKind.LANDING_PAGE,
                url=f"https://arxiv.org/abs/{full_id}",
                access_status=AccessStatus.OPEN,
                mime_type="text/html",
            ),
        ]
    return PaperRecord(
        provider="arxiv",
        provider_record_id=arxiv_id,
        external_ids={
            "doi": f"10.1000/fixture-{index}",
            "arxiv": arxiv_id,
            "arxiv_version": version,
        },
        title=f"Fixture Paper {index}",
        authors=[f"Author {index}", f"Coauthor {index}"],
        year=2024,
        venue=f"Journal {index}" if index != 2 else "arXiv",
        abstract=f"Fixture abstract {index}.",
        categories=["cs.AI", "cs.CL"],
        record_url=f"https://arxiv.org/abs/{full_id}",
        access_locations=locations,
        source_query=source_query,
        retrieved_at=SNAPSHOT_AT,
    )


def make_provider(client: httpx.AsyncClient, **overrides) -> ArxivProvider:
    options = {
        "client": client,
        "request_gate": ArxivRequestGate(min_interval_seconds=0),
        "now_factory": lambda: SNAPSHOT_AT,
    }
    options.update(overrides)
    return ArxivProvider(**options)


class TestArxivProviderContract:
    async def test_shared_provider_contract_and_request_shape(self) -> None:
        versions = {1: 2, 2: 1, 3: None}
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            params = request.url.params
            if "id_list" in params:
                return atom_response(atom_feed([atom_entry(1, version=2)], total=1))
            start = int(params["start"])
            page_size = int(params["max_results"])
            indexes = list(range(1, 4))[start : start + page_size]
            return atom_response(
                atom_feed(
                    [atom_entry(index, version=versions[index]) for index in indexes],
                    total=3,
                    start=start,
                    items_per_page=page_size,
                )
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(client)
            records = [
                expected_record(1, version=2),
                expected_record(2, version=1),
                expected_record(3),
            ]
            await assert_provider_contract(
                provider,
                records,
                query_text=QUERY_TEXT,
            )

        first_request = requests[0]
        assert first_request.url.scheme == "https"
        assert first_request.url.host == "export.arxiv.org"
        assert first_request.url.params["search_query"] == QUERY_TEXT
        assert first_request.url.params["start"] == "0"
        assert first_request.url.params["max_results"] == "2"
        assert first_request.url.params["sortBy"] == "relevance"
        assert first_request.url.params["sortOrder"] == "descending"
        assert first_request.headers["User-Agent"] == "ScholarAssistant-PoC/0.1"

    async def test_search_translates_filters_sorting_and_pagination(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return atom_response(atom_feed([atom_entry(1, version=2)], total=11, start=10))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(client)
            page = await provider.search(
                SearchQuery(
                    query="all:agents",
                    page=2,
                    page_size=10,
                    sort_by=SearchSortBy.YEAR,
                    sort_order=SearchSortOrder.ASCENDING,
                    filters=SearchFilters(
                        year_from=2022,
                        year_to=2024,
                        categories=["cs.AI", "cs.CL"],
                    ),
                )
            )

        params = captured[0].url.params
        assert params["search_query"] == (
            "(all:agents) AND (cat:cs.AI OR cat:cs.CL) AND "
            "submittedDate:[202201010000 TO 202412312359]"
        )
        assert params["start"] == "10"
        assert params["sortBy"] == "submittedDate"
        assert params["sortOrder"] == "ascending"
        assert len(page.records) == 1
        assert page.records[0].source_query == params["search_query"]
        assert page.result_mode is SearchResultMode.LIVE

    async def test_zero_results_is_valid(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([], total=0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            page = await make_provider(client).search(SearchQuery(query="all:unmatched"))

        assert page.total_results == 0
        assert page.records == []
        assert page.has_more is False

    async def test_rejects_unsafe_category_before_network(self) -> None:
        called = False

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return atom_response(atom_feed([], total=0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(client)
            with pytest.raises(LiteratureProviderError) as exc_info:
                await provider.search(
                    SearchQuery(
                        query="all:agents",
                        filters=SearchFilters(categories=["cs.AI OR all:*"]),
                    )
                )

        assert called is False
        assert exc_info.value.code is ProviderErrorCode.INVALID_REQUEST


class TestArxivRecordLookup:
    async def test_get_record_normalizes_url_and_preserves_requested_version(self) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return atom_response(atom_feed([atom_entry(1, version=2)], total=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            record = await make_provider(client).get_record(
                "https://arxiv.org/pdf/2401.10001v2.pdf"
            )

        assert captured[0].url.params["id_list"] == "2401.10001v2"
        assert record.paper_id == expected_record(1, version=2).paper_id
        assert record.external_ids.arxiv_version == 2
        assert record.source_query == "id_list:2401.10001v2"

    async def test_get_record_accepts_legacy_identifier(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([legacy_atom_entry()], total=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            record = await make_provider(client).get_record("hep-ex/0307015v2")

        assert record.provider_record_id == "hep-ex/0307015"
        assert record.external_ids.arxiv_version == 2
        assert record.year == 2003
        assert record.authors == ["H1 Collaboration"]

    async def test_versions_share_paper_identity_but_change_metadata_snapshot(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            requested_id = request.url.params["id_list"]
            version = 1 if requested_id.endswith("v1") else 2
            return atom_response(atom_feed([atom_entry(1, version=version)], total=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(client)
            version_1 = await provider.get_record("2401.10001v1")
            version_2 = await provider.get_record("2401.10001v2")

        assert version_1.paper_id == version_2.paper_id
        assert version_1.metadata_snapshot_hash != version_2.metadata_snapshot_hash

    async def test_get_record_reports_valid_missing_id_as_not_found(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([], total=0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).get_record("2401.99999")

        assert exc_info.value.code is ProviderErrorCode.NOT_FOUND
        assert exc_info.value.operation is ProviderOperation.GET_RECORD

    async def test_get_record_rejects_invalid_identifier_without_network(self) -> None:
        called = False

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return atom_response(atom_feed([], total=0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).get_record("not-an-arxiv-id")

        assert called is False
        assert exc_info.value.code is ProviderErrorCode.INVALID_REQUEST
        assert exc_info.value.status_code == 400

    async def test_get_record_rejects_version_mismatch(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([atom_entry(1, version=2)], total=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).get_record("2401.10001v1")

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    @pytest.mark.parametrize(
        ("total", "entry_index"),
        [
            (2, 1),
            (1, 2),
        ],
    )
    async def test_get_record_rejects_inconsistent_lookup_response(
        self,
        total: int,
        entry_index: int,
    ) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([atom_entry(entry_index, version=2)], total=total))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).get_record("2401.10001v2")

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    async def test_resolve_access_builds_canonical_locations_when_absent(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: None)
        ) as client:
            provider = make_provider(client)
            record = expected_record(1, version=2, access_locations=[])
            locations = await provider.resolve_access(record)

        assert [location.kind for location in locations] == [
            AccessKind.PDF,
            AccessKind.LANDING_PAGE,
        ]
        assert locations[0].url == "https://arxiv.org/pdf/2401.10001v2"

    async def test_resolve_access_rejects_record_from_another_provider(self) -> None:
        foreign = PaperRecord(
            provider="other",
            provider_record_id="record-1",
            title="Other record",
            authors=["Author"],
            year=2024,
            record_url="https://example.com/record-1",
            source_query="query",
            retrieved_at=SNAPSHOT_AT,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: None)
        ) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).resolve_access(foreign)

        assert exc_info.value.code is ProviderErrorCode.NOT_FOUND
        assert exc_info.value.operation is ProviderOperation.RESOLVE_ACCESS

    async def test_resolve_access_rejects_mismatched_arxiv_identity(self) -> None:
        payload = expected_record(1).model_dump(mode="json")
        payload["external_ids"]["arxiv"] = "2401.10002"
        payload["metadata_snapshot_hash"] = ""
        mismatched = PaperRecord.model_validate(payload)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: None)
        ) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).resolve_access(mismatched)

        assert exc_info.value.code is ProviderErrorCode.INVALID_REQUEST


class TestArxivFailures:
    @pytest.mark.parametrize(
        ("status_code", "expected_code", "retryable", "retry_after"),
        [
            (429, ProviderErrorCode.RATE_LIMITED, True, 7.0),
            (503, ProviderErrorCode.UNAVAILABLE, True, None),
            (400, ProviderErrorCode.INVALID_REQUEST, False, None),
        ],
    )
    async def test_http_failures_are_typed(
        self,
        status_code: int,
        expected_code: ProviderErrorCode,
        retryable: bool,
        retry_after: float | None,
    ) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            headers = {"Retry-After": "7"} if status_code == 429 else {}
            return httpx.Response(status_code, text="upstream failure", headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        error = exc_info.value
        assert error.code is expected_code
        assert error.status_code == status_code
        assert error.retryable is retryable
        assert error.retry_after_seconds == retry_after

    async def test_transport_failure_is_unavailable_not_empty_results(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.UNAVAILABLE
        assert exc_info.value.operation is ProviderOperation.SEARCH

    async def test_timeout_is_unavailable_not_empty_results(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.UNAVAILABLE
        assert "timed out" in str(exc_info.value)

    async def test_http_not_found_during_lookup_is_typed(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="missing")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).get_record("2401.10001")

        assert exc_info.value.code is ProviderErrorCode.NOT_FOUND
        assert exc_info.value.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            "not xml",
            "<feed />",
            """<?xml version="1.0"?>
            <!DOCTYPE feed [<!ENTITY x "unsafe">]>
            <feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>""",
        ],
    )
    async def test_invalid_atom_is_explicit_failure(self, body: str) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE
        assert exc_info.value.operation is ProviderOperation.SEARCH

    async def test_utf16_doctype_is_rejected(self) -> None:
        body = """<?xml version="1.0" encoding="UTF-16"?>
        <!DOCTYPE feed [<!ENTITY x "unsafe">]>
        <feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>""".encode("utf-16")

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=body,
                headers={"Content-Type": "application/atom+xml"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    async def test_unexpected_success_content_type_is_invalid_response(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([], total=0), content_type="text/html")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    async def test_missing_required_entry_metadata_is_invalid_response(self) -> None:
        incomplete_entry = """
        <entry>
          <id>http://arxiv.org/abs/2401.10001v1</id>
          <published>2024-01-01T12:00:00Z</published>
          <title>Missing authors and summary</title>
        </entry>
        """

        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([incomplete_entry], total=1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    @pytest.mark.parametrize(
        ("body", "query"),
        [
            (
                atom_feed([atom_entry(1)], total=1, start=1),
                SearchQuery(query="all:agents"),
            ),
            (
                atom_feed([atom_entry(1)], total=10, start=20),
                SearchQuery(query="all:agents", page=2, page_size=20),
            ),
        ],
    )
    async def test_pagination_contradictions_are_invalid_response(
        self,
        body: str,
        query: SearchQuery,
    ) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(query)

        assert exc_info.value.code is ProviderErrorCode.INVALID_RESPONSE

    @pytest.mark.parametrize(
        ("body", "query", "expected_total", "expected_records", "expected_has_more"),
        [
            (
                atom_feed([], total=1, start=0),
                SearchQuery(query="all:agents"),
                1,
                0,
                True,
            ),
            (
                atom_feed([atom_entry(1)], total=100, start=0),
                SearchQuery(query="all:agents", page_size=20),
                100,
                1,
                True,
            ),
        ],
    )
    async def test_short_page_is_accepted_because_total_results_is_an_estimate(
        self,
        body: str,
        query: SearchQuery,
        expected_total: int,
        expected_records: int,
        expected_has_more: bool,
    ) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            page = await make_provider(client).search(query)

        assert page.total_results == expected_total
        assert len(page.records) == expected_records
        assert page.has_more is expected_has_more

    async def test_atom_error_entry_uses_client_error_status(self) -> None:
        body = atom_feed(
            [
                """<entry><id>http://arxiv.org/api/errors#bad_query</id>
                <title>Error</title><summary>bad query</summary></entry>"""
            ],
            total=1,
        )

        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(LiteratureProviderError) as exc_info:
                await make_provider(client).search(SearchQuery(query="bad query"))

        assert exc_info.value.code is ProviderErrorCode.INVALID_REQUEST
        assert exc_info.value.status_code == 400

    async def test_unusable_client_configuration_is_a_typed_unavailable_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken_client(_self: ArxivProvider) -> httpx.AsyncClient:
            raise httpx.InvalidURL("Invalid port: ':1]'")

        monkeypatch.setattr(ArxivProvider, "_get_client", broken_client)
        provider = ArxivProvider(
            request_gate=ArxivRequestGate(min_interval_seconds=0),
            now_factory=lambda: SNAPSHOT_AT,
        )

        with pytest.raises(LiteratureProviderError) as exc_info:
            await provider.search(SearchQuery(query="all:agents"))

        assert exc_info.value.code is ProviderErrorCode.UNAVAILABLE
        assert exc_info.value.retryable is True
        assert exc_info.value.details["exception_type"] == "InvalidURL"


class TestArxivRequestPolicy:
    async def test_default_gate_is_shared_within_one_event_loop(self) -> None:
        assert arxiv_module._default_request_gate() is arxiv_module._default_request_gate()

    async def test_default_gate_keeps_the_documented_three_second_interval(self) -> None:
        gate = arxiv_module._default_request_gate()

        assert arxiv_module.ARXIV_MIN_REQUEST_INTERVAL_SECONDS == 3.0
        assert gate._min_interval_seconds == arxiv_module.ARXIV_MIN_REQUEST_INTERVAL_SECONDS

    async def test_owned_client_is_closed_while_injected_client_is_left_open(self) -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([], total=0))

        owned = ArxivProvider(
            request_gate=ArxivRequestGate(min_interval_seconds=0),
            now_factory=lambda: SNAPSHOT_AT,
        )
        owned._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await owned.search(SearchQuery(query="all:agents"))
        assert owned._client is not None
        assert owned._client.is_closed is False

        await owned.aclose()
        assert owned._client is None

        injected_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with injected_client:
            await make_provider(injected_client).aclose()
            assert injected_client.is_closed is False

    async def test_shared_gate_serializes_multiple_provider_instances(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum_active = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if not entered.is_set():
                entered.set()
                await release.wait()
            active -= 1
            return atom_response(atom_feed([], total=0))

        gate = ArxivRequestGate(min_interval_seconds=0)
        transport = httpx.MockTransport(handler)
        async with (
            httpx.AsyncClient(transport=transport) as first_client,
            httpx.AsyncClient(transport=transport) as second_client,
        ):
            first = make_provider(first_client, request_gate=gate)
            second = make_provider(second_client, request_gate=gate)
            first_task = asyncio.create_task(first.search(SearchQuery(query="all:first")))
            await entered.wait()
            second_task = asyncio.create_task(second.search(SearchQuery(query="all:second")))
            await asyncio.sleep(0)
            assert maximum_active == 1
            release.set()
            await asyncio.gather(first_task, second_task)

        assert maximum_active == 1

    async def test_sequential_requests_observe_configured_interval(
        self,
    ) -> None:
        clock = iter([10.0, 11.0, 13.0])
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        gate = ArxivRequestGate(
            min_interval_seconds=3.0,
            clock=lambda: next(clock),
            sleeper=fake_sleep,
        )

        async def handler(_request: httpx.Request) -> httpx.Response:
            return atom_response(atom_feed([], total=0))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = make_provider(client, request_gate=gate)
            await provider.search(SearchQuery(query="all:first"))
            await provider.search(SearchQuery(query="all:second"))

        assert sleeps == [2.0]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"endpoint": "http://export.arxiv.org/api/query"},
            {"timeout_seconds": 0},
            {"user_agent": "   "},
        ],
    )
    def test_invalid_transport_configuration_is_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            ArxivProvider(**kwargs)

    def test_negative_gate_interval_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ArxivRequestGate(min_interval_seconds=-1)
