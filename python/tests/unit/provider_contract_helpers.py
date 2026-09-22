"""Reusable behavioral checks for every LiteratureProvider implementation."""

from __future__ import annotations

from src.literature import LiteratureProvider, PaperRecord, SearchQuery, SearchResultMode


async def assert_provider_contract(
    provider: LiteratureProvider,
    records: list[PaperRecord],
    *,
    query_text: str,
) -> None:
    assert isinstance(provider, LiteratureProvider)
    capabilities = provider.capabilities()
    assert capabilities.provider == provider.name
    assert capabilities.supports_search
    assert capabilities.supports_record_lookup
    assert capabilities.supports_access_resolution

    query = SearchQuery(query=query_text, page_size=2)
    first_page = await provider.search(query)
    repeated_page = await provider.search(query)

    assert first_page.provider == provider.name
    assert first_page.result_mode is capabilities.result_mode
    if first_page.result_mode is not SearchResultMode.LIVE:
        assert first_page.provenance_label
    assert first_page.total_results == len(records)
    assert first_page.has_more is (len(records) > query.page_size)
    assert [item.paper_id for item in first_page.records] == [
        record.paper_id for record in records[: query.page_size]
    ]
    assert repeated_page.result_snapshot_id == first_page.result_snapshot_id

    fetched = await provider.get_record(records[0].provider_record_id)
    assert fetched.paper_id == first_page.records[0].paper_id
    assert fetched.metadata_snapshot_hash == records[0].metadata_snapshot_hash

    locations = await provider.resolve_access(fetched)
    assert locations == fetched.access_locations


__all__ = ["assert_provider_contract"]
