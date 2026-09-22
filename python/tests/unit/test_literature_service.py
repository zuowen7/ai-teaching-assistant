"""Application-service tests for literature search snapshots and project import."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.literature.models import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    PaperRecord,
    ProviderCapabilities,
    SearchGenerationMethod,
    SearchPage,
    SearchPlanDraft,
    SearchQuery,
    SearchResultMode,
)
from src.literature.providers.base import (
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)
from src.literature.service import (
    ImportDisposition,
    LiteratureService,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
)

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
QUERY = 'all:"research assistance"'


class MemoryProjectStore:
    def __init__(self) -> None:
        self.projects: dict[str, list[dict[str, Any]]] = {}
        self.replace_count = 0
        self.fail_replace = False

    def update_sources(self, project_path: str, update) -> None:
        current = deepcopy(self.projects.setdefault(project_path, []))
        sources = update(current)
        if self.fail_replace:
            raise OSError("simulated atomic replace failure")
        if sources != current:
            self.projects[project_path] = deepcopy(sources)
            self.replace_count += 1


class StaticProvider:
    def __init__(
        self,
        name: str,
        records: Sequence[PaperRecord],
        *,
        error: LiteratureProviderError | None = None,
    ) -> None:
        self._name = name
        self.records = list(records)
        self.error = error
        self.last_query: SearchQuery | None = None
        self.closed = False
        self.snapshot_at = NOW

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            result_mode=SearchResultMode.FIXTURE,
            supports_fulltext_download=False,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        if self.error is not None:
            raise self.error
        self.last_query = query
        start = (query.page - 1) * query.page_size
        page_records = self.records[start : start + query.page_size]
        return SearchPage(
            provider=self.name,
            result_mode=SearchResultMode.FIXTURE,
            query=query,
            records=page_records,
            total_results=len(self.records),
            has_more=start + len(page_records) < len(self.records),
            retrieved_at=self.snapshot_at,
            provenance_label=f"{self.name}-test-snapshot",
        )

    async def get_record(self, external_id: str) -> PaperRecord:
        for record in self.records:
            if external_id in {record.paper_id, record.provider_record_id}:
                return record
        raise LiteratureProviderError(
            ProviderErrorCode.NOT_FOUND,
            "not found",
            provider=self.name,
            operation=ProviderOperation.GET_RECORD,
        )

    async def resolve_access(self, record: PaperRecord) -> list[AccessLocation]:
        return list(record.access_locations)

    async def aclose(self) -> None:
        self.closed = True


def make_record(
    provider: str,
    record_id: str,
    *,
    doi: str | None = None,
    arxiv: str | None = None,
    title: str | None = None,
    author: str = "Ada Researcher",
    year: int = 2025,
    venue: str | None = None,
    abstract: str = "Evidence-grounded research assistance.",
    categories: Sequence[str] = ("cs.AI",),
    with_access: bool = True,
    access_url: str | None = None,
    retrieved_at: datetime = NOW,
) -> PaperRecord:
    return PaperRecord(
        provider=provider,
        provider_record_id=record_id,
        external_ids=ExternalIdentifiers(doi=doi, arxiv=arxiv),
        title=title or f"Paper {record_id}",
        authors=[author],
        year=year,
        venue=venue,
        abstract=abstract,
        categories=list(categories),
        record_url=f"https://example.test/{provider}/{record_id}",
        access_locations=(
            [
                AccessLocation(
                    kind=AccessKind.PDF,
                    url=access_url or f"https://example.test/{provider}/{record_id}.pdf",
                    access_status=AccessStatus.OPEN,
                    mime_type="application/pdf",
                    is_primary=True,
                )
            ]
            if with_access
            else []
        ),
        source_query=QUERY,
        retrieved_at=retrieved_at,
    )


def make_service(
    providers: Sequence[StaticProvider],
    store: MemoryProjectStore,
    *,
    ids: Sequence[str] = ("src_lit_0000000000000001", "src_lit_0000000000000002"),
    snapshot_capacity: int = 64,
) -> LiteratureService:
    pending_ids = iter(ids)
    return LiteratureService(
        providers=providers,
        project_store=store,
        snapshot_capacity=snapshot_capacity,
        now_factory=lambda: NOW,
        source_id_factory=lambda: next(pending_ids),
    )


def make_plan(
    *,
    research_question: str = "How is AI used for research assistance?",
    suggested_query: str = QUERY,
) -> SearchPlanDraft:
    return SearchPlanDraft(
        research_question=research_question,
        suggested_query=suggested_query,
        generation_method=SearchGenerationMethod.TEMPLATE,
        generation_config={"template": "all_phrase_v1"},
    )


async def search(
    service: LiteratureService,
    provider: str,
    *,
    research_question: str = "How is AI used for research assistance?",
):
    return await service.search(
        provider,
        SearchQuery(query=QUERY),
        plan=make_plan(research_question=research_question),
    )


async def test_search_passes_confirmed_query_and_preserves_valid_empty_page() -> None:
    store = MemoryProjectStore()
    provider = StaticProvider("alpha", [])
    service = make_service([provider], store)
    query = SearchQuery(query='ti:"multi agent"', page=2, page_size=5)

    execution = await service.search(
        " ALPHA ",
        query,
        plan=make_plan(suggested_query=query.query),
    )

    assert provider.last_query == query
    assert execution.page.records == []
    assert execution.page.total_results == 0
    assert execution.page.result_mode is SearchResultMode.FIXTURE
    assert execution.plan.executed_query == query
    assert execution.plan.result_snapshot_id == execution.page.result_snapshot_id
    assert execution.search_execution_id.startswith("search_exec_")
    assert len(execution.search_execution_id) == len("search_exec_") + 32


async def test_search_requires_a_plan() -> None:
    service = make_service([StaticProvider("alpha", [])], MemoryProjectStore())

    with pytest.raises(TypeError, match="plan"):
        await service.search("alpha", SearchQuery(query=QUERY))  # type: ignore[call-arg]


async def test_provider_failure_is_not_converted_to_empty_results() -> None:
    error = LiteratureProviderError(
        ProviderErrorCode.UNAVAILABLE,
        "upstream body must not become an empty page",
        provider="alpha",
        operation=ProviderOperation.SEARCH,
    )
    service = make_service([StaticProvider("alpha", [], error=error)], MemoryProjectStore())

    with pytest.raises(LiteratureProviderError) as captured:
        await search(service, "alpha")

    assert captured.value.code is ProviderErrorCode.UNAVAILABLE


async def test_search_rejects_record_owned_by_a_different_provider() -> None:
    foreign_record = make_record("beta", "B1")
    service = make_service([StaticProvider("alpha", [foreign_record])], MemoryProjectStore())

    with pytest.raises(LiteratureProviderError) as captured:
        await search(service, "alpha")

    assert captured.value.code is ProviderErrorCode.INVALID_RESPONSE


async def test_search_rejects_duplicate_paper_ids_before_snapshot_storage() -> None:
    first = make_record("alpha", "A1", doi="10.1/first")
    conflicting = make_record("alpha", "A1", doi="10.1/conflict")
    service = make_service(
        [StaticProvider("alpha", [first, conflicting])],
        MemoryProjectStore(),
    )

    with pytest.raises(LiteratureProviderError) as captured:
        await search(service, "alpha")

    assert captured.value.code is ProviderErrorCode.INVALID_RESPONSE


async def test_import_creates_metadata_only_project_source() -> None:
    record = make_record("alpha", "A1", doi="https://doi.org/10.1/EXAMPLE", arxiv="2501.00001v2")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    execution = await search(service, "alpha")

    batch = await service.import_selection(
        project_path="project-a",
        search_execution_id=execution.search_execution_id,
        paper_ids=[record.paper_id],
    )

    assert batch.created_count == 1
    assert batch.search_execution_id == execution.search_execution_id
    assert batch.result_snapshot_id == execution.page.result_snapshot_id
    assert store.replace_count == 1
    source = store.projects["project-a"][0]
    assert source["id"] != record.paper_id
    assert source["original_path"] is None
    assert source["rag_status"] == "unavailable"
    literature = source["metadata"]["literature"]
    assert literature["records"][record.paper_id]["metadata_snapshot_hash"]
    assert literature["search_snapshot_ids"] == [execution.page.result_snapshot_id]
    assert literature["fulltext"] == {
        "source_id": source["id"],
        "status": "metadata_only",
        "artifact_id": None,
        "local_path": None,
        "source_url": "https://example.test/alpha/A1.pdf",
        "access_status": "open",
        "acquired_at": None,
        "file_size_bytes": None,
        "mime_type": None,
        "sha256": None,
        "artifact_version": 1,
        "failure_reason": None,
    }


async def test_import_persists_server_finalized_search_plan() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    draft = SearchPlanDraft(
        research_question="How are agents used in academic writing?",
        suggested_query='all:"agents academic writing"',
        generation_method=SearchGenerationMethod.TEMPLATE,
        generation_config={"template": "all_phrase_v1"},
    )

    execution = await service.search(
        "alpha",
        SearchQuery(query=QUERY),
        plan=draft,
    )
    await service.import_selection(
        project_path="project-a",
        search_execution_id=execution.search_execution_id,
        paper_ids=[record.paper_id],
    )

    event = store.projects["project-a"][0]["metadata"]["literature"]["search_events"][0]
    plan = event["search_plan"]
    assert plan["research_question"] == "How are agents used in academic writing?"
    assert plan["suggested_query"] == 'all:"agents academic writing"'
    assert plan["generation_method"] == "template"
    assert plan["generation_config"] == {"template": "all_phrase_v1"}
    assert plan["provider"] == "alpha"
    assert plan["executed_query"]["query"] == QUERY
    confirmed_at = datetime.fromisoformat(plan["confirmed_at"].replace("Z", "+00:00"))
    executed_at = datetime.fromisoformat(plan["executed_at"].replace("Z", "+00:00"))
    assert confirmed_at == NOW
    assert executed_at == NOW
    assert event["search_execution_id"] == execution.search_execution_id
    assert plan["result_snapshot_id"] == execution.page.result_snapshot_id


async def test_exact_repeat_is_idempotent_and_does_not_refresh_timestamps() -> None:
    record = make_record("alpha", "A1", doi="10.1/example")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")
    first = await service.import_selection(
        project_path="project-a",
        search_execution_id=page.search_execution_id,
        paper_ids=[record.paper_id],
    )
    before = deepcopy(store.projects["project-a"])

    repeated = await service.import_selection(
        project_path="project-a",
        search_execution_id=page.search_execution_id,
        paper_ids=[record.paper_id],
    )

    assert first.results[0].disposition is ImportDisposition.CREATED
    assert repeated.results[0].disposition is ImportDisposition.REUSED
    assert store.projects["project-a"] == before
    assert store.replace_count == 1


async def test_same_snapshot_keeps_distinct_execution_plans_when_imported_later() -> None:
    record = make_record("alpha", "A1", doi="10.1/example")
    provider = StaticProvider("alpha", [record])
    store = MemoryProjectStore()
    service = make_service([provider], store)
    first_page = await search(
        service,
        "alpha",
        research_question="How do agents plan academic writing?",
    )
    provider.snapshot_at = NOW + timedelta(minutes=5)
    second_page = await search(
        service,
        "alpha",
        research_question="How do agents review academic writing?",
    )
    assert second_page.page.result_snapshot_id == first_page.page.result_snapshot_id
    assert second_page.search_execution_id != first_page.search_execution_id

    first_import = await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[record.paper_id],
    )
    second_import = await service.import_selection(
        project_path="project-a",
        search_execution_id=second_page.search_execution_id,
        paper_ids=[record.paper_id],
    )

    assert first_import.search_execution_id == first_page.search_execution_id
    assert second_import.search_execution_id == second_page.search_execution_id
    assert first_import.result_snapshot_id == second_import.result_snapshot_id
    assert second_import.results[0].disposition is ImportDisposition.METADATA_UPDATED
    events = store.projects["project-a"][0]["metadata"]["literature"]["search_events"]
    assert [event["retrieved_at"] for event in events] == [
        NOW.isoformat(),
        (NOW + timedelta(minutes=5)).isoformat(),
    ]
    plans_by_execution = {
        event["search_execution_id"]: event["search_plan"]["research_question"] for event in events
    }
    assert plans_by_execution == {
        first_page.search_execution_id: "How do agents plan academic writing?",
        second_page.search_execution_id: "How do agents review academic writing?",
    }
    assert all(event["query"]["query"] == QUERY for event in events)
    assert all(event["source_query"] == QUERY for event in events)


async def test_historical_events_remain_valid_after_repeated_record_refreshes() -> None:
    versions = [
        make_record(
            "alpha",
            "A1",
            abstract=f"Metadata version {version}.",
            retrieved_at=NOW + timedelta(minutes=version),
        )
        for version in range(1, 4)
    ]
    assert len({record.paper_id for record in versions}) == 1
    assert len({record.metadata_snapshot_hash for record in versions}) == 3
    provider = StaticProvider("alpha", [versions[0]])
    provider.snapshot_at = versions[0].retrieved_at
    store = MemoryProjectStore()
    service = make_service([provider], store)

    for record in versions:
        provider.records = [record]
        provider.snapshot_at = record.retrieved_at
        execution = await search(service, "alpha")
        imported = await service.import_selection(
            project_path="project-a",
            search_execution_id=execution.search_execution_id,
            paper_ids=[record.paper_id],
        )

    assert imported.results[0].disposition is ImportDisposition.METADATA_UPDATED
    literature = store.projects["project-a"][0]["metadata"]["literature"]
    events = literature["search_events"]
    assert [event["metadata_snapshot_hash"] for event in events] == [
        record.metadata_snapshot_hash for record in versions
    ]
    assert [event["record_retrieved_at"] for event in events] == [
        record.retrieved_at.isoformat() for record in versions
    ]
    saved = literature["records"][versions[0].paper_id]
    assert saved["metadata_snapshot_hash"] == versions[-1].metadata_snapshot_hash


async def test_batch_duplicate_is_written_once_with_stable_result_order() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    batch = await service.import_selection(
        project_path="project-a",
        search_execution_id=page.search_execution_id,
        paper_ids=[record.paper_id, record.paper_id],
    )

    assert [item.disposition for item in batch.results] == [
        ImportDisposition.CREATED,
        ImportDisposition.REUSED,
    ]
    assert batch.results[0].source_id == batch.results[1].source_id
    assert len(store.projects["project-a"]) == 1
    assert store.replace_count == 1


async def test_all_batch_results_contain_the_final_persisted_source_state() -> None:
    first = make_record("alpha", "A1", doi="10.1/same")
    second = make_record("alpha", "A2", doi="10.1/same")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [first, second])], store)
    page = await search(service, "alpha")

    batch = await service.import_selection(
        project_path="project-a",
        search_execution_id=page.search_execution_id,
        paper_ids=[first.paper_id, second.paper_id],
    )

    persisted = store.projects["project-a"][0]
    assert batch.results[0].source == persisted
    assert batch.results[1].source == persisted
    assert set(persisted["metadata"]["literature"]["records"]) == {
        first.paper_id,
        second.paper_id,
    }


async def test_same_doi_across_providers_merges_metadata_without_overwriting_user_state() -> None:
    first = make_record("alpha", "A1", doi="DOI:10.1/SAME")
    second = make_record("beta", "B9", doi="https://doi.org/10.1/same")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [second])],
        store,
    )
    first_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )
    source = store.projects["project-a"][0]
    source.update(
        {
            "original_path": "C:/project/references/paper.pdf",
            "rag_status": "ready",
            "reading_status": "read",
            "cited": True,
        }
    )
    source["metadata"]["tags"] = ["keep-me"]
    source["metadata"]["custom"] = {"keep": True}
    preserved_created_at = source["created_at"]
    second_page = await search(service, "beta")

    merged = await service.import_selection(
        project_path="project-a",
        search_execution_id=second_page.search_execution_id,
        paper_ids=[second.paper_id],
    )

    assert merged.results[0].disposition is ImportDisposition.METADATA_UPDATED
    assert merged.results[0].matched_by == "doi"
    saved = store.projects["project-a"][0]
    assert saved["created_at"] == preserved_created_at
    assert saved["original_path"] == "C:/project/references/paper.pdf"
    assert saved["rag_status"] == "ready"
    assert saved["reading_status"] == "read"
    assert saved["cited"] is True
    assert saved["metadata"]["tags"] == ["keep-me"]
    assert saved["metadata"]["custom"] == {"keep": True}
    assert set(saved["metadata"]["literature"]["records"]) == {
        first.paper_id,
        second.paper_id,
    }


async def test_same_doi_merge_adds_new_open_fulltext_url_before_acquisition() -> None:
    first = make_record("alpha", "A1", doi="10.1/same", with_access=False)
    second = make_record("beta", "B9", doi="10.1/same")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [second])],
        store,
    )
    first_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )
    first_fulltext = store.projects["project-a"][0]["metadata"]["literature"]["fulltext"]
    assert first_fulltext["source_url"] is None
    assert first_fulltext["access_status"] == "unknown"

    second_page = await search(service, "beta")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=second_page.search_execution_id,
        paper_ids=[second.paper_id],
    )

    fulltext = store.projects["project-a"][0]["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "metadata_only"
    assert fulltext["source_url"] == "https://example.test/beta/B9.pdf"
    assert fulltext["access_status"] == "open"


async def test_same_doi_merge_does_not_rewrite_active_or_failed_fulltext_state() -> None:
    for status, failure_reason in (("acquiring", None), ("acquire_failed", "network error")):
        first = make_record("alpha", f"A-{status}", doi=f"10.1/{status}", with_access=False)
        second = make_record("beta", f"B-{status}", doi=f"10.1/{status}")
        store = MemoryProjectStore()
        service = make_service(
            [StaticProvider("alpha", [first]), StaticProvider("beta", [second])],
            store,
        )
        first_page = await search(service, "alpha")
        await service.import_selection(
            project_path="project-a",
            search_execution_id=first_page.search_execution_id,
            paper_ids=[first.paper_id],
        )
        fulltext = store.projects["project-a"][0]["metadata"]["literature"]["fulltext"]
        fulltext["status"] = status
        fulltext["failure_reason"] = failure_reason
        preserved = dict(fulltext)

        second_page = await search(service, "beta")
        await service.import_selection(
            project_path="project-a",
            search_execution_id=second_page.search_execution_id,
            paper_ids=[second.paper_id],
        )

        assert store.projects["project-a"][0]["metadata"]["literature"]["fulltext"] == preserved


async def test_arxiv_base_id_is_a_strong_deduplication_key() -> None:
    first = make_record("alpha", "A1", arxiv="2501.00001v1")
    second = make_record("beta", "B1", arxiv="arXiv:2501.00001v3")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [second])], store
    )
    first_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )
    second_page = await search(service, "beta")

    result = await service.import_selection(
        project_path="project-a",
        search_execution_id=second_page.search_execution_id,
        paper_ids=[second.paper_id],
    )

    assert result.results[0].matched_by == "arxiv"
    assert len(store.projects["project-a"]) == 1


async def test_newer_arxiv_version_refreshes_metadata_only_fulltext_url() -> None:
    first = make_record(
        "arxiv",
        "2501.00001",
        arxiv="2501.00001v1",
        access_url="https://arxiv.org/pdf/2501.00001v1",
    )
    latest = make_record(
        "arxiv",
        "2501.00001",
        arxiv="2501.00001v3",
        access_url="https://arxiv.org/pdf/2501.00001v3",
    )
    provider = StaticProvider("arxiv", [first])
    store = MemoryProjectStore()
    service = make_service([provider], store)
    first_page = await search(service, "arxiv")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )

    provider.records = [latest]
    latest_page = await search(service, "arxiv")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=latest_page.search_execution_id,
        paper_ids=[latest.paper_id],
    )

    literature = store.projects["project-a"][0]["metadata"]["literature"]
    saved_record = literature["records"][first.paper_id]
    assert saved_record["external_ids"]["arxiv_version"] == 3
    assert literature["fulltext"]["source_url"] == "https://arxiv.org/pdf/2501.00001v3"


async def test_older_arxiv_version_cannot_downgrade_record_or_fulltext_url() -> None:
    latest = make_record(
        "arxiv",
        "2501.00001",
        arxiv="2501.00001v3",
        access_url="https://arxiv.org/pdf/2501.00001v3",
    )
    older = make_record(
        "arxiv",
        "2501.00001",
        arxiv="2501.00001v1",
        access_url="https://arxiv.org/pdf/2501.00001v1",
    )
    provider = StaticProvider("arxiv", [latest])
    store = MemoryProjectStore()
    service = make_service([provider], store)
    latest_page = await search(service, "arxiv")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=latest_page.search_execution_id,
        paper_ids=[latest.paper_id],
    )

    provider.records = [older]
    older_page = await search(service, "arxiv")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=older_page.search_execution_id,
        paper_ids=[older.paper_id],
    )

    literature = store.projects["project-a"][0]["metadata"]["literature"]
    saved_record = literature["records"][latest.paper_id]
    assert saved_record["external_ids"]["arxiv_version"] == 3
    assert literature["fulltext"]["source_url"] == "https://arxiv.org/pdf/2501.00001v3"


async def test_less_complete_refresh_cannot_erase_existing_metadata() -> None:
    complete = make_record(
        "alpha",
        "A1",
        doi="10.1/keep",
        arxiv="2501.00001v2",
        venue="PoC Journal",
        abstract="Detailed abstract.",
        categories=("cs.AI", "cs.CL"),
    )
    incomplete = make_record(
        "alpha",
        "A1",
        abstract="",
        categories=(),
        with_access=False,
    )
    matching_other_provider = make_record("beta", "B1", doi="10.1/keep")
    alpha = StaticProvider("alpha", [complete])
    beta = StaticProvider("beta", [matching_other_provider])
    store = MemoryProjectStore()
    service = make_service([alpha, beta], store)
    complete_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=complete_page.search_execution_id,
        paper_ids=[complete.paper_id],
    )

    alpha.records = [incomplete]
    incomplete_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=incomplete_page.search_execution_id,
        paper_ids=[incomplete.paper_id],
    )
    stored_record = store.projects["project-a"][0]["metadata"]["literature"]["records"]
    refreshed = stored_record[complete.paper_id]
    assert refreshed["external_ids"]["doi"] == "10.1/keep"
    assert refreshed["external_ids"]["arxiv"] == "2501.00001"
    assert refreshed["venue"] == "PoC Journal"
    assert refreshed["abstract"] == "Detailed abstract."
    assert refreshed["categories"] == ["cs.AI", "cs.CL"]
    assert refreshed["access_locations"] == complete.model_dump(mode="json")["access_locations"]

    beta_page = await search(service, "beta")
    merged = await service.import_selection(
        project_path="project-a",
        search_execution_id=beta_page.search_execution_id,
        paper_ids=[matching_other_provider.paper_id],
    )
    assert merged.results[0].matched_by == "doi"
    assert len(store.projects["project-a"]) == 1


async def test_title_author_year_only_reports_candidate_and_never_auto_merges() -> None:
    first = make_record("alpha", "A1", title="Same Title")
    second = make_record("beta", "B1", title="  same   title  ")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [second])], store
    )
    first_page = await search(service, "alpha")
    first_import = await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )
    second_page = await search(service, "beta")

    second_import = await service.import_selection(
        project_path="project-a",
        search_execution_id=second_page.search_execution_id,
        paper_ids=[second.paper_id],
    )

    assert second_import.results[0].disposition is ImportDisposition.CREATED
    assert second_import.results[0].possible_duplicate_source_ids == [
        first_import.results[0].source_id
    ]
    assert len(store.projects["project-a"]) == 2


async def test_split_strong_identity_conflict_rejects_whole_batch_without_write() -> None:
    by_doi = make_record("alpha", "A1", doi="10.1/first")
    by_arxiv = make_record("beta", "B1", arxiv="2501.00002")
    conflict = make_record(
        "gamma",
        "C1",
        doi="10.1/first",
        arxiv="2501.00002",
    )
    safe = make_record("gamma", "C2", doi="10.1/safe")
    store = MemoryProjectStore()
    service = make_service(
        [
            StaticProvider("alpha", [by_doi]),
            StaticProvider("beta", [by_arxiv]),
            StaticProvider("gamma", [safe, conflict]),
        ],
        store,
        ids=(
            "src_lit_0000000000000001",
            "src_lit_0000000000000002",
            "src_lit_0000000000000003",
        ),
    )
    for provider_name, record in [("alpha", by_doi), ("beta", by_arxiv)]:
        page = await search(service, provider_name)
        await service.import_selection(
            project_path="project-a",
            search_execution_id=page.search_execution_id,
            paper_ids=[record.paper_id],
        )
    before = deepcopy(store.projects["project-a"])
    writes_before = store.replace_count
    conflict_page = await search(service, "gamma")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=conflict_page.search_execution_id,
            paper_ids=[safe.paper_id, conflict.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.IDENTITY_CONFLICT
    assert store.projects["project-a"] == before
    assert store.replace_count == writes_before


async def test_failed_atomic_replace_leaves_original_manifest_unchanged() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    store.projects["project-a"] = [
        {
            "id": "src_existing",
            "title": "Existing",
            "metadata": {},
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    ]
    before = deepcopy(store.projects["project-a"])
    store.fail_replace = True
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    with pytest.raises(OSError, match="simulated atomic replace failure"):
        await service.import_selection(
            project_path="project-a",
            search_execution_id=page.search_execution_id,
            paper_ids=[record.paper_id],
        )

    assert store.projects["project-a"] == before


async def test_same_paper_can_exist_with_distinct_source_ids_in_two_projects() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    first = await service.import_selection(
        project_path="project-a",
        search_execution_id=page.search_execution_id,
        paper_ids=[record.paper_id],
    )
    second = await service.import_selection(
        project_path="project-b",
        search_execution_id=page.search_execution_id,
        paper_ids=[record.paper_id],
    )

    assert first.results[0].source_id != second.results[0].source_id
    assert len(store.projects["project-a"]) == 1
    assert len(store.projects["project-b"]) == 1


async def test_missing_or_evicted_execution_is_rejected_before_store_write() -> None:
    first = make_record("alpha", "A1")
    second = make_record("beta", "B1")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [second])],
        store,
        snapshot_capacity=1,
    )
    first_page = await search(service, "alpha")
    await search(service, "beta")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=first_page.search_execution_id,
            paper_ids=[first.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.SEARCH_EXECUTION_NOT_FOUND
    assert store.replace_count == 0


async def test_record_outside_snapshot_is_rejected() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=page.search_execution_id,
            paper_ids=["paper_not_in_snapshot"],
        )

    assert captured.value.code is LiteratureServiceErrorCode.RECORD_NOT_IN_SNAPSHOT
    assert store.replace_count == 0


async def test_search_plan_that_disagrees_with_event_is_rejected_before_write() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [record])], store)
    first_execution = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_execution.search_execution_id,
        paper_ids=[record.paper_id],
    )
    literature = store.projects["project-a"][0]["metadata"]["literature"]
    literature["search_events"][0]["search_plan"]["provider"] = "tampered-provider"
    writes_before = store.replace_count
    second_execution = await search(service, "alpha")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=second_execution.search_execution_id,
            paper_ids=[record.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.PROJECT_DATA_INVALID
    assert store.replace_count == writes_before


async def test_same_execution_cannot_bind_different_plans_across_sources() -> None:
    first = make_record("alpha", "A1")
    second = make_record("alpha", "A2")
    store = MemoryProjectStore()
    service = make_service([StaticProvider("alpha", [first, second])], store)
    execution = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=execution.search_execution_id,
        paper_ids=[first.paper_id, second.paper_id],
    )
    assert len(store.projects["project-a"]) == 2
    second_literature = store.projects["project-a"][1]["metadata"]["literature"]
    second_literature["search_events"][0]["search_plan"]["research_question"] = (
        "Tampered research question"
    )
    writes_before = store.replace_count
    next_execution = await search(service, "alpha")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=next_execution.search_execution_id,
            paper_ids=[first.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.PROJECT_DATA_INVALID
    assert store.replace_count == writes_before


async def test_corrupt_literature_metadata_is_not_silently_ignored() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    store.projects["project-a"] = [
        {
            "id": "src_corrupt",
            "title": "Corrupt",
            "metadata": {"literature": {"records": []}},
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    ]
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=page.search_execution_id,
            paper_ids=[record.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.PROJECT_DATA_INVALID
    assert store.replace_count == 0


async def test_fulltext_state_must_validate_and_belong_to_its_project_source() -> None:
    record = make_record("alpha", "A1")
    store = MemoryProjectStore()
    store.projects["project-a"] = [
        {
            "id": "src_owner",
            "title": "Wrong owner",
            "metadata": {
                "literature": {
                    "schema_version": 1,
                    "primary_paper_id": record.paper_id,
                    "records": {record.paper_id: record.model_dump(mode="json")},
                    "search_snapshot_ids": [],
                    "search_events": [],
                    "fulltext": {
                        "source_id": "src_other",
                        "status": "metadata_only",
                        "access_status": "unknown",
                        "artifact_version": 1,
                    },
                }
            },
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    ]
    service = make_service([StaticProvider("alpha", [record])], store)
    page = await search(service, "alpha")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=page.search_execution_id,
            paper_ids=[record.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.PROJECT_DATA_INVALID
    assert store.replace_count == 0


async def test_existing_source_with_conflicting_strong_identities_is_rejected() -> None:
    first = make_record("alpha", "A1", doi="10.1/first")
    conflicting = make_record("beta", "B1", doi="10.1/conflict")
    store = MemoryProjectStore()
    service = make_service(
        [StaticProvider("alpha", [first]), StaticProvider("beta", [conflicting])],
        store,
    )
    first_page = await search(service, "alpha")
    await service.import_selection(
        project_path="project-a",
        search_execution_id=first_page.search_execution_id,
        paper_ids=[first.paper_id],
    )
    literature = store.projects["project-a"][0]["metadata"]["literature"]
    literature["records"][conflicting.paper_id] = conflicting.model_dump(mode="json")
    writes_before = store.replace_count
    conflict_page = await search(service, "beta")

    with pytest.raises(LiteratureServiceError) as captured:
        await service.import_selection(
            project_path="project-a",
            search_execution_id=conflict_page.search_execution_id,
            paper_ids=[conflicting.paper_id],
        )

    assert captured.value.code is LiteratureServiceErrorCode.PROJECT_DATA_INVALID
    assert store.replace_count == writes_before


async def test_service_closes_owned_provider_resources() -> None:
    provider = StaticProvider("alpha", [])
    service = make_service([provider], MemoryProjectStore())

    await service.aclose()

    assert provider.closed is True
