"""Application service for literature discovery and project import.

The service owns provider selection, bounded search executions, conservative
identity matching, and one-write project imports.  It deliberately does not
download, parse, index, or answer from full text; those are later PoC phases.
"""

from __future__ import annotations

import inspect
import re
import unicodedata
import uuid
from asyncio import Lock
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.literature.models import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    FullTextArtifact,
    FullTextStatus,
    PaperRecord,
    ProviderCapabilities,
    SearchPage,
    SearchPlan,
    SearchPlanDraft,
    SearchQuery,
)
from src.literature.providers.base import (
    LiteratureProvider,
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)

_SOURCE_SCHEMA_VERSION = 1
_MAX_SNAPSHOT_IDS_PER_SOURCE = 50
_MAX_SOURCE_ID_ATTEMPTS = 20
_MAX_SEARCH_EXECUTION_ID_ATTEMPTS = 20
_SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_SEARCH_EXECUTION_ID_RE = re.compile(r"^search_exec_[0-9a-f]{32}$")


@runtime_checkable
class ProjectSourceStore(Protocol):
    """Minimal project persistence boundary required by the service."""

    def update_sources(
        self,
        project_path: str,
        update: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        """Run ``update`` inside one project-manifest read-modify-write lock."""
        ...


class LiteratureServiceErrorCode(StrEnum):
    UNKNOWN_PROVIDER = "unknown_provider"
    SEARCH_EXECUTION_NOT_FOUND = "search_execution_not_found"
    RECORD_NOT_IN_SNAPSHOT = "record_not_in_snapshot"
    IDENTITY_CONFLICT = "identity_conflict"
    PROJECT_DATA_INVALID = "project_data_invalid"
    SOURCE_ID_EXHAUSTED = "source_id_exhausted"
    SEARCH_EXECUTION_ID_EXHAUSTED = "search_execution_id_exhausted"


class LiteratureServiceError(RuntimeError):
    """Controlled application failure with no transport-specific status code."""

    def __init__(
        self,
        code: LiteratureServiceErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = deepcopy(dict(details or {}))


class ImportDisposition(StrEnum):
    CREATED = "created"
    REUSED = "reused"
    METADATA_UPDATED = "metadata_updated"


class IdentityKind(StrEnum):
    DOI = "doi"
    ARXIV = "arxiv"
    PAPER_ID = "paper_id"


class LiteratureImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str
    source_id: str
    disposition: ImportDisposition
    matched_by: IdentityKind | None = None
    possible_duplicate_source_ids: list[str] = Field(default_factory=list)
    source: dict[str, Any]


class LiteratureImportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    search_execution_id: str = Field(pattern=r"^search_exec_[0-9a-f]{32}$")
    result_snapshot_id: str = Field(pattern=r"^search_[0-9a-f]{24}$")
    results: list[LiteratureImportItem]
    created_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    metadata_updated_count: int = Field(ge=0)


class LiteratureSearchExecution(BaseModel):
    """One server-owned search invocation bound to its exact plan and result page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_execution_id: str = Field(pattern=r"^search_exec_[0-9a-f]{32}$")
    page: SearchPage
    plan: SearchPlan

    @model_validator(mode="after")
    def validate_binding(self) -> LiteratureSearchExecution:
        if (
            self.plan.provider != self.page.provider
            or self.plan.executed_query != self.page.query
            or self.plan.result_snapshot_id != self.page.result_snapshot_id
        ):
            raise ValueError("search plan does not match the executed search page")
        return self


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalized_doi(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return ExternalIdentifiers(doi=str(value)).doi
    except (TypeError, ValueError):
        return None


def _normalized_arxiv(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return ExternalIdentifiers(arxiv=str(value)).arxiv
    except (TypeError, ValueError):
        return None


def _record_identities(record: PaperRecord) -> dict[IdentityKind, str]:
    identities = {IdentityKind.PAPER_ID: record.paper_id}
    if record.external_ids.doi:
        identities[IdentityKind.DOI] = record.external_ids.doi
    if record.external_ids.arxiv:
        identities[IdentityKind.ARXIV] = record.external_ids.arxiv
    return identities


def _candidate_key(record: PaperRecord) -> tuple[str, str, int] | None:
    if record.year is None or not record.authors:
        return None
    return (_normalized_text(record.title), _normalized_text(record.authors[0]), record.year)


def _source_candidate_keys(source: Mapping[str, Any]) -> set[tuple[str, str, int]]:
    keys: set[tuple[str, str, int]] = set()
    for record in _source_records(source, strict=False):
        key = _candidate_key(record)
        if key is not None:
            keys.add(key)

    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return keys
    authors = metadata.get("authors")
    raw_year = metadata.get("year")
    title = source.get("title")
    if isinstance(authors, list) and authors and isinstance(title, str):
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            return keys
        first_author = authors[0]
        if isinstance(first_author, str) and 1000 <= year <= 3000:
            keys.add((_normalized_text(title), _normalized_text(first_author), year))
    return keys


def _source_records(
    source: Mapping[str, Any],
    *,
    strict: bool,
) -> list[PaperRecord]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    literature = metadata.get("literature")
    if literature is None:
        return []
    if not isinstance(literature, Mapping):
        if strict:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                "项目文献的 literature metadata 不是对象",
                details={"source_id": source.get("id")},
            )
        return []
    raw_records = literature.get("records", {})
    if not isinstance(raw_records, Mapping):
        if strict:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                "项目文献的 records metadata 不是对象",
                details={"source_id": source.get("id")},
            )
        return []

    records: list[PaperRecord] = []
    for paper_id, payload in raw_records.items():
        try:
            record = PaperRecord.model_validate(payload)
        except (TypeError, ValueError) as exc:
            if strict:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献包含无效的规范化记录",
                    details={"source_id": source.get("id"), "paper_id": str(paper_id)},
                ) from exc
            continue
        if str(paper_id) != record.paper_id:
            if strict:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献的记录键与 paper_id 不一致",
                    details={"source_id": source.get("id"), "paper_id": str(paper_id)},
                )
            continue
        records.append(record)
    return records


def _legacy_source_identities(source: Mapping[str, Any]) -> dict[IdentityKind, set[str]]:
    identities: dict[IdentityKind, set[str]] = defaultdict(set)
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return identities

    paper_id = metadata.get("paper_id") or metadata.get("primary_paper_id")
    if isinstance(paper_id, str) and paper_id.strip():
        identities[IdentityKind.PAPER_ID].add(paper_id.strip())

    raw_external = metadata.get("external_ids")
    external = raw_external if isinstance(raw_external, Mapping) else {}
    doi = _normalized_doi(metadata.get("doi") or metadata.get("DOI") or external.get("doi"))
    if doi:
        identities[IdentityKind.DOI].add(doi)
    arxiv = _normalized_arxiv(
        metadata.get("arxiv")
        or metadata.get("arxiv_id")
        or metadata.get("arXiv")
        or external.get("arxiv")
    )
    if arxiv:
        identities[IdentityKind.ARXIV].add(arxiv)
    return identities


def _source_identities(
    source: Mapping[str, Any],
    *,
    strict: bool,
) -> dict[IdentityKind, set[str]]:
    identities = _legacy_source_identities(source)
    for record in _source_records(source, strict=strict):
        for kind, value in _record_identities(record).items():
            identities[kind].add(value)
    return identities


def _preferred_access(record: PaperRecord) -> AccessLocation | None:
    if not record.access_locations:
        return None
    primary = next((location for location in record.access_locations if location.is_primary), None)
    if primary is not None:
        return primary
    pdf = next(
        (location for location in record.access_locations if location.kind is AccessKind.PDF), None
    )
    return pdf or record.access_locations[0]


def _initial_fulltext(source_id: str, record: PaperRecord) -> FullTextArtifact:
    access = _preferred_access(record)
    return FullTextArtifact(
        source_id=source_id,
        status=FullTextStatus.METADATA_ONLY,
        source_url=access.url if access is not None else None,
        access_status=access.access_status if access is not None else AccessStatus.UNKNOWN,
    )


def _merge_discovered_fulltext(
    existing: FullTextArtifact,
    *,
    record: PaperRecord,
) -> FullTextArtifact:
    """Monotonically add a better acquisition URL before acquisition starts."""

    if existing.status is not FullTextStatus.METADATA_ONLY:
        return existing

    discovered = _initial_fulltext(existing.source_id, record)
    if discovered.source_url is None:
        return existing

    access_rank = {
        AccessStatus.UNKNOWN: 0,
        AccessStatus.RESTRICTED: 1,
        AccessStatus.OPEN: 2,
    }
    should_replace = (
        existing.source_url is None
        or (access_rank[discovered.access_status] > access_rank[existing.access_status])
        or (
            access_rank[discovered.access_status] == access_rank[existing.access_status]
            and discovered.source_url != existing.source_url
        )
    )
    if not should_replace:
        return existing

    return FullTextArtifact.model_validate(
        {
            **existing.model_dump(mode="python"),
            "source_url": discovered.source_url,
            "access_status": discovered.access_status,
        }
    )


def _display_title(title: str) -> tuple[str, bool]:
    if len(title) <= 500:
        return title, False
    return title[:499].rstrip() + "…", True


def _search_event(
    execution: LiteratureSearchExecution,
    record: PaperRecord,
) -> dict[str, Any]:
    """Persist enough data to interpret a selected search after process restart."""

    page = execution.page
    return {
        "search_execution_id": execution.search_execution_id,
        "result_snapshot_id": page.result_snapshot_id,
        "provider": page.provider,
        "result_mode": page.result_mode.value,
        "query": page.query.model_dump(mode="json"),
        "retrieved_at": page.retrieved_at.isoformat(),
        "provenance_label": page.provenance_label,
        "total_results": page.total_results,
        "paper_id": record.paper_id,
        "metadata_snapshot_hash": record.metadata_snapshot_hash,
        "source_query": record.source_query,
        "record_retrieved_at": record.retrieved_at.isoformat(),
        "search_plan": execution.plan.model_dump(mode="json"),
    }


def _merge_record_refresh(
    existing: PaperRecord,
    incoming: PaperRecord,
) -> PaperRecord:
    """Refresh one provider record without erasing richer established metadata."""

    same_arxiv = (
        incoming.external_ids.arxiv is not None
        and incoming.external_ids.arxiv == existing.external_ids.arxiv
    )
    incoming_version = incoming.external_ids.arxiv_version
    existing_version = existing.external_ids.arxiv_version
    if (
        same_arxiv
        and incoming_version is not None
        and existing_version is not None
        and incoming_version < existing_version
    ):
        return existing

    payload = incoming.model_dump(mode="json")
    external = dict(payload["external_ids"])
    if not incoming.external_ids.doi and existing.external_ids.doi:
        external["doi"] = existing.external_ids.doi
    if not incoming.external_ids.arxiv and existing.external_ids.arxiv:
        external["arxiv"] = existing.external_ids.arxiv
        external["arxiv_version"] = existing.external_ids.arxiv_version
    elif (
        incoming.external_ids.arxiv == existing.external_ids.arxiv
        and incoming.external_ids.arxiv_version is None
        and existing.external_ids.arxiv_version is not None
    ):
        external["arxiv_version"] = existing.external_ids.arxiv_version
    external["other"] = {
        **existing.external_ids.other,
        **incoming.external_ids.other,
    }
    payload["external_ids"] = external
    if incoming.year is None and existing.year is not None:
        payload["year"] = existing.year
    if incoming.venue is None and existing.venue is not None:
        payload["venue"] = existing.venue
    if not incoming.abstract and existing.abstract:
        payload["abstract"] = existing.abstract
    if not incoming.categories and existing.categories:
        payload["categories"] = list(existing.categories)
    if not incoming.access_locations and existing.access_locations:
        payload["access_locations"] = [
            location.model_dump(mode="json") for location in existing.access_locations
        ]
    payload["metadata_snapshot_hash"] = ""
    return PaperRecord.model_validate(payload)


class LiteratureService:
    """Coordinate replaceable providers and conservative project imports."""

    def __init__(
        self,
        *,
        providers: Sequence[LiteratureProvider],
        project_store: ProjectSourceStore,
        snapshot_capacity: int = 64,
        now_factory: Callable[[], datetime] | None = None,
        source_id_factory: Callable[[], str] | None = None,
        search_execution_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if snapshot_capacity < 1:
            raise ValueError("snapshot_capacity must be positive")
        registry: dict[str, LiteratureProvider] = {}
        for provider in providers:
            name = provider.name.strip().casefold()
            if not name:
                raise ValueError("provider name cannot be blank")
            if name in registry:
                raise ValueError(f"duplicate literature provider: {name}")
            capabilities = provider.capabilities()
            if capabilities.provider != name:
                raise ValueError("provider capabilities name does not match provider.name")
            registry[name] = provider
        if not registry:
            raise ValueError("at least one literature provider is required")
        if not isinstance(project_store, ProjectSourceStore):
            raise TypeError("project_store does not satisfy ProjectSourceStore")

        self._providers = registry
        self._project_store = project_store
        self._execution_capacity = snapshot_capacity
        self._search_executions: OrderedDict[str, LiteratureSearchExecution] = OrderedDict()
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._source_id_factory = source_id_factory or (lambda: f"src_lit_{uuid.uuid4().hex[:16]}")
        self._search_execution_id_factory = search_execution_id_factory or (
            lambda: f"search_exec_{uuid.uuid4().hex}"
        )
        self._import_lock = Lock()

    def capabilities(self) -> list[ProviderCapabilities]:
        return [self._providers[name].capabilities() for name in sorted(self._providers)]

    async def search(
        self,
        provider_name: str,
        query: SearchQuery,
        *,
        plan: SearchPlanDraft,
    ) -> LiteratureSearchExecution:
        provider = self._provider(provider_name)
        validated_query = SearchQuery.model_validate(query.model_dump(mode="json"))
        validated_plan = SearchPlanDraft.model_validate(plan.model_dump(mode="json"))
        confirmed_at = self._now_datetime()
        page = await provider.search(validated_query)
        snapshot = SearchPage.model_validate(page.model_dump(mode="json"))
        if snapshot.provider != provider.name:
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned a search page for a different provider",
                provider=provider.name,
                operation=ProviderOperation.SEARCH,
            )
        if snapshot.query != validated_query:
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned a search page for a different query",
                provider=provider.name,
                operation=ProviderOperation.SEARCH,
            )
        if any(record.provider != provider.name for record in snapshot.records):
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned a record owned by a different provider",
                provider=provider.name,
                operation=ProviderOperation.SEARCH,
            )
        paper_ids = [record.paper_id for record in snapshot.records]
        if len(set(paper_ids)) != len(paper_ids):
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned duplicate paper identities in one search page",
                provider=provider.name,
                operation=ProviderOperation.SEARCH,
            )
        finalized_plan = SearchPlan.model_validate(
            {
                **validated_plan.model_dump(mode="python"),
                "executed_query": validated_query,
                "provider": provider.name,
                "confirmed_at": confirmed_at,
                "executed_at": self._now_datetime(),
                "result_snapshot_id": snapshot.result_snapshot_id,
            }
        )
        execution = LiteratureSearchExecution(
            search_execution_id=self._new_search_execution_id(),
            page=snapshot,
            plan=finalized_plan,
        )
        self._remember_execution(execution)
        return execution

    async def get_record(self, provider_name: str, external_id: str) -> PaperRecord:
        provider = self._provider(provider_name)
        record = await provider.get_record(external_id)
        validated = PaperRecord.model_validate(record.model_dump(mode="json"))
        if validated.provider != provider.name:
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "provider returned a record owned by a different provider",
                provider=provider.name,
                operation=ProviderOperation.GET_RECORD,
            )
        return validated

    async def import_selection(
        self,
        *,
        project_path: str,
        search_execution_id: str,
        paper_ids: Sequence[str],
    ) -> LiteratureImportBatch:
        execution = self._search_executions.get(search_execution_id)
        if execution is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SEARCH_EXECUTION_NOT_FOUND,
                "检索执行不存在或已过期，请重新检索",
                details={"search_execution_id": search_execution_id},
            )
        snapshot = execution.page
        if not paper_ids:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.RECORD_NOT_IN_SNAPSHOT,
                "至少选择一篇检索结果",
            )

        records_by_id = {record.paper_id: record for record in snapshot.records}
        selected: list[PaperRecord] = []
        missing: list[str] = []
        for paper_id in paper_ids:
            record = records_by_id.get(paper_id)
            if record is None:
                missing.append(paper_id)
            else:
                selected.append(record)
        if missing:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.RECORD_NOT_IN_SNAPSHOT,
                "所选论文不属于该检索快照",
                details={"paper_ids": list(dict.fromkeys(missing))},
            )

        async with self._import_lock:
            results: list[LiteratureImportItem] = []

            def update(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
                planned_sources, planned_results = self._plan_import(
                    sources=deepcopy(sources),
                    records=selected,
                    search_execution=execution,
                )
                results.extend(planned_results)
                return planned_sources

            self._project_store.update_sources(project_path, update)

        return LiteratureImportBatch(
            search_execution_id=search_execution_id,
            result_snapshot_id=snapshot.result_snapshot_id,
            results=results,
            created_count=sum(item.disposition is ImportDisposition.CREATED for item in results),
            reused_count=sum(item.disposition is ImportDisposition.REUSED for item in results),
            metadata_updated_count=sum(
                item.disposition is ImportDisposition.METADATA_UPDATED for item in results
            ),
        )

    async def aclose(self) -> None:
        failures: list[Exception] = []
        seen: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "aclose", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - lifecycle caller logs failures
                failures.append(exc)
        if failures:
            raise RuntimeError(
                f"failed to close {len(failures)} literature provider(s)"
            ) from failures[0]

    def _provider(self, name: str) -> LiteratureProvider:
        key = name.strip().casefold()
        provider = self._providers.get(key)
        if provider is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.UNKNOWN_PROVIDER,
                f"未知文献源: {name}",
                details={"provider": key},
            )
        return provider

    def _remember_execution(self, execution: LiteratureSearchExecution) -> None:
        self._search_executions[execution.search_execution_id] = execution
        self._search_executions.move_to_end(execution.search_execution_id)
        while len(self._search_executions) > self._execution_capacity:
            self._search_executions.popitem(last=False)

    def _plan_import(
        self,
        *,
        sources: list[dict[str, Any]],
        records: Sequence[PaperRecord],
        search_execution: LiteratureSearchExecution,
    ) -> tuple[list[dict[str, Any]], list[LiteratureImportItem]]:
        self._validate_sources(sources)
        planned = deepcopy(sources)
        results: list[LiteratureImportItem] = []

        # Preflight all incoming strong identities against the original manifest and
        # each other before constructing any write payload.
        self._preflight_conflicts(planned, records)

        for record in records:
            identity_index = self._identity_index(planned)
            matches: dict[IdentityKind, str] = {}
            for kind, value in _record_identities(record).items():
                source_ids = identity_index.get((kind, value), set())
                if len(source_ids) > 1:
                    raise self._identity_conflict(record, source_ids)
                if source_ids:
                    matches[kind] = next(iter(source_ids))

            matched_source_ids = set(matches.values())
            if len(matched_source_ids) > 1:
                raise self._identity_conflict(record, matched_source_ids)

            candidate_ids = self._candidate_source_ids(planned, record)
            if matched_source_ids:
                source_id = next(iter(matched_source_ids))
                source_index = next(
                    index for index, source in enumerate(planned) if source.get("id") == source_id
                )
                existing = planned[source_index]
                self._ensure_identity_compatible(existing, record)
                updated = self._merge_record(
                    existing,
                    record,
                    _search_event(search_execution, record),
                )
                matched_by = min(matches, key=self._identity_rank)
                if updated == existing:
                    disposition = ImportDisposition.REUSED
                else:
                    disposition = ImportDisposition.METADATA_UPDATED
                    planned[source_index] = updated
                results.append(
                    LiteratureImportItem(
                        paper_id=record.paper_id,
                        source_id=source_id,
                        disposition=disposition,
                        matched_by=matched_by,
                        possible_duplicate_source_ids=[],
                        source=deepcopy(planned[source_index]),
                    )
                )
                continue

            source_id = self._new_source_id(planned)
            source = self._new_source(
                source_id=source_id,
                record=record,
                search_event=_search_event(search_execution, record),
            )
            planned.insert(0, source)
            results.append(
                LiteratureImportItem(
                    paper_id=record.paper_id,
                    source_id=source_id,
                    disposition=ImportDisposition.CREATED,
                    matched_by=None,
                    possible_duplicate_source_ids=candidate_ids,
                    source=deepcopy(source),
                )
            )
        final_sources = {str(source["id"]): source for source in planned}
        final_results = [
            LiteratureImportItem.model_validate(
                {
                    **item.model_dump(mode="python", exclude={"source"}),
                    "source": deepcopy(final_sources[item.source_id]),
                }
            )
            for item in results
        ]
        return planned, final_results

    @staticmethod
    def _identity_rank(kind: IdentityKind) -> int:
        return {
            IdentityKind.DOI: 0,
            IdentityKind.ARXIV: 1,
            IdentityKind.PAPER_ID: 2,
        }[kind]

    def _preflight_conflicts(
        self,
        sources: Sequence[Mapping[str, Any]],
        records: Sequence[PaperRecord],
    ) -> None:
        index = self._identity_index(sources)
        incoming: dict[tuple[IdentityKind, str], PaperRecord] = {}
        for record in records:
            matched_source_ids: set[str] = set()
            for kind, value in _record_identities(record).items():
                source_ids = index.get((kind, value), set())
                if len(source_ids) > 1:
                    raise self._identity_conflict(record, source_ids)
                matched_source_ids.update(source_ids)
                other = incoming.get((kind, value))
                if other is not None:
                    self._ensure_records_compatible(other, record)
                else:
                    incoming[(kind, value)] = record
            if len(matched_source_ids) > 1:
                raise self._identity_conflict(record, matched_source_ids)
            if matched_source_ids:
                source_id = next(iter(matched_source_ids))
                source = next(item for item in sources if item.get("id") == source_id)
                self._ensure_identity_compatible(source, record)

    @staticmethod
    def _validate_sources(sources: Sequence[Mapping[str, Any]]) -> None:
        seen: set[str] = set()
        execution_bindings: dict[str, dict[str, Any]] = {}
        for source in sources:
            source_id = source.get("id")
            if not isinstance(source_id, str) or not _SAFE_SOURCE_ID_RE.fullmatch(source_id):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献包含无效 source_id",
                )
            if source_id in seen:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献包含重复 source_id",
                    details={"source_id": source_id},
                )
            seen.add(source_id)
            records = {record.paper_id: record for record in _source_records(source, strict=True)}
            identities = _source_identities(source, strict=True)
            if (
                len(identities.get(IdentityKind.DOI, set())) > 1
                or len(identities.get(IdentityKind.ARXIV, set())) > 1
            ):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "同一项目文献包含互相冲突的强身份",
                    details={"source_id": source_id},
                )
            metadata = source.get("metadata")
            literature = metadata.get("literature") if isinstance(metadata, Mapping) else None
            if literature is None:
                continue
            if not isinstance(literature, Mapping):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献的 literature metadata 不是对象",
                    details={"source_id": source_id},
                )
            if literature.get("schema_version") != _SOURCE_SCHEMA_VERSION:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献使用了不支持的 literature schema",
                    details={"source_id": source_id},
                )
            raw_records = literature.get("records")
            primary_paper_id = literature.get("primary_paper_id")
            if (
                not isinstance(raw_records, Mapping)
                or not isinstance(primary_paper_id, str)
                or primary_paper_id not in raw_records
            ):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献的 primary_paper_id 无法解析",
                    details={"source_id": source_id},
                )
            snapshot_ids = literature.get("search_snapshot_ids", [])
            search_events = literature.get("search_events", [])
            if not isinstance(snapshot_ids, list) or any(
                not isinstance(item, str) for item in snapshot_ids
            ):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献的检索快照列表无效",
                    details={"source_id": source_id},
                )
            if not isinstance(search_events, list) or any(
                not isinstance(item, Mapping) for item in search_events
            ):
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献的检索事件列表无效",
                    details={"source_id": source_id},
                )
            for event in search_events:
                event_snapshot_id = event.get("result_snapshot_id")
                event_paper_id = event.get("paper_id")
                event_metadata_hash = event.get("metadata_snapshot_hash")
                event_source_query = event.get("source_query")
                event_record_retrieved_at = event.get("record_retrieved_at")
                event_provider = str(event.get("provider", "")).strip().casefold()
                try:
                    event_query = SearchQuery.model_validate(event.get("query"))
                    record_retrieved_at = datetime.fromisoformat(
                        str(event_record_retrieved_at).replace("Z", "+00:00")
                    )
                except (TypeError, ValueError) as exc:
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献包含无效的检索事件",
                        details={"source_id": source_id},
                    ) from exc
                record = records.get(event_paper_id) if isinstance(event_paper_id, str) else None
                if (
                    not isinstance(event_snapshot_id, str)
                    or not re.fullmatch(r"search_[0-9a-f]{24}", event_snapshot_id)
                    or event_snapshot_id not in snapshot_ids
                    or record is None
                    or record.provider != event_provider
                    or not isinstance(event_metadata_hash, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", event_metadata_hash)
                    or not isinstance(event_source_query, str)
                    or not event_source_query.strip()
                    or record_retrieved_at.tzinfo is None
                    or record_retrieved_at.utcoffset() is None
                ):
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献的检索事件与规范化记录不一致",
                        details={"source_id": source_id},
                    )
                raw_plan = event.get("search_plan")
                if raw_plan is None:
                    if event.get("search_execution_id") is not None:
                        raise LiteratureServiceError(
                            LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                            "项目文献的检索执行缺少检索计划",
                            details={"source_id": source_id},
                        )
                    continue
                event_execution_id = event.get("search_execution_id")
                if not isinstance(
                    event_execution_id, str
                ) or not _SAFE_SEARCH_EXECUTION_ID_RE.fullmatch(event_execution_id):
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献包含无效的检索执行标识",
                        details={"source_id": source_id},
                    )
                try:
                    plan = SearchPlan.model_validate(raw_plan)
                except (TypeError, ValueError) as exc:
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献包含无效的检索计划",
                        details={"source_id": source_id},
                    ) from exc
                if (
                    plan.provider != event_provider
                    or plan.result_snapshot_id != event_snapshot_id
                    or plan.executed_query != event_query
                    or event_snapshot_id not in snapshot_ids
                ):
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献的检索计划与检索事件不一致",
                        details={"source_id": source_id},
                    )
                binding = {
                    "plan": plan.model_dump(mode="json"),
                    "provider": event_provider,
                    "result_snapshot_id": event_snapshot_id,
                    "result_mode": event.get("result_mode"),
                    "query": event_query.model_dump(mode="json"),
                    "retrieved_at": event.get("retrieved_at"),
                    "provenance_label": event.get("provenance_label"),
                    "total_results": event.get("total_results"),
                }
                established = execution_bindings.setdefault(event_execution_id, binding)
                if established != binding:
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "同一检索执行标识绑定了不一致的检索计划或结果",
                        details={"source_id": source_id},
                    )
            try:
                fulltext = FullTextArtifact.model_validate(literature.get("fulltext"))
            except (TypeError, ValueError) as exc:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献包含无效的全文状态",
                    details={"source_id": source_id},
                ) from exc
            if fulltext.source_id != source_id:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "全文状态不属于当前项目文献",
                    details={"source_id": source_id},
                )

    @staticmethod
    def _identity_index(
        sources: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[IdentityKind, str], set[str]]:
        index: dict[tuple[IdentityKind, str], set[str]] = defaultdict(set)
        for source in sources:
            source_id = str(source["id"])
            for kind, values in _source_identities(source, strict=True).items():
                for value in values:
                    index[(kind, value)].add(source_id)
        return index

    @staticmethod
    def _candidate_source_ids(
        sources: Sequence[Mapping[str, Any]],
        record: PaperRecord,
    ) -> list[str]:
        key = _candidate_key(record)
        if key is None:
            return []
        return [str(source["id"]) for source in sources if key in _source_candidate_keys(source)]

    @staticmethod
    def _ensure_records_compatible(first: PaperRecord, second: PaperRecord) -> None:
        first_doi = first.external_ids.doi
        second_doi = second.external_ids.doi
        if first_doi and second_doi and first_doi != second_doi:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.IDENTITY_CONFLICT,
                "同一强身份对应不同 DOI，已拒绝入库",
                details={"paper_ids": [first.paper_id, second.paper_id]},
            )
        first_arxiv = first.external_ids.arxiv
        second_arxiv = second.external_ids.arxiv
        if first_arxiv and second_arxiv and first_arxiv != second_arxiv:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.IDENTITY_CONFLICT,
                "同一强身份对应不同 arXiv ID，已拒绝入库",
                details={"paper_ids": [first.paper_id, second.paper_id]},
            )

    def _ensure_identity_compatible(
        self,
        source: Mapping[str, Any],
        record: PaperRecord,
    ) -> None:
        existing = _source_identities(source, strict=True)
        incoming = _record_identities(record)
        if (
            incoming.get(IdentityKind.DOI)
            and existing.get(IdentityKind.DOI)
            and incoming[IdentityKind.DOI] not in existing[IdentityKind.DOI]
        ):
            raise self._identity_conflict(record, {str(source["id"])})
        if (
            incoming.get(IdentityKind.ARXIV)
            and existing.get(IdentityKind.ARXIV)
            and incoming[IdentityKind.ARXIV] not in existing[IdentityKind.ARXIV]
        ):
            raise self._identity_conflict(record, {str(source["id"])})

    @staticmethod
    def _identity_conflict(
        record: PaperRecord,
        source_ids: set[str],
    ) -> LiteratureServiceError:
        return LiteratureServiceError(
            LiteratureServiceErrorCode.IDENTITY_CONFLICT,
            "文献强身份发生冲突，已拒绝入库",
            details={"paper_id": record.paper_id, "source_ids": sorted(source_ids)},
        )

    def _new_source_id(self, sources: Sequence[Mapping[str, Any]]) -> str:
        existing = {str(source.get("id")) for source in sources}
        for _ in range(_MAX_SOURCE_ID_ATTEMPTS):
            candidate = self._source_id_factory().strip()
            if _SAFE_SOURCE_ID_RE.fullmatch(candidate) and candidate not in existing:
                return candidate
        raise LiteratureServiceError(
            LiteratureServiceErrorCode.SOURCE_ID_EXHAUSTED,
            "无法生成唯一的项目文献 ID",
        )

    def _new_search_execution_id(self) -> str:
        for _ in range(_MAX_SEARCH_EXECUTION_ID_ATTEMPTS):
            candidate = self._search_execution_id_factory().strip()
            if (
                _SAFE_SEARCH_EXECUTION_ID_RE.fullmatch(candidate)
                and candidate not in self._search_executions
            ):
                return candidate
        raise LiteratureServiceError(
            LiteratureServiceErrorCode.SEARCH_EXECUTION_ID_EXHAUSTED,
            "无法生成唯一的检索执行标识",
        )

    def _new_source(
        self,
        *,
        source_id: str,
        record: PaperRecord,
        search_event: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        title, title_truncated = _display_title(record.title)
        fulltext = _initial_fulltext(source_id, record)
        metadata: dict[str, Any] = {
            "source_kind": "literature",
            "authors": list(record.authors),
            "year": str(record.year) if record.year is not None else None,
            "journal": record.venue,
            "tags": [],
            "literature": {
                "schema_version": _SOURCE_SCHEMA_VERSION,
                "primary_paper_id": record.paper_id,
                "records": {record.paper_id: record.model_dump(mode="json")},
                "search_snapshot_ids": [search_event["result_snapshot_id"]],
                "search_events": [deepcopy(dict(search_event))],
                "fulltext": fulltext.model_dump(mode="json"),
                "display_title_truncated": title_truncated,
            },
        }
        return {
            "id": source_id,
            "title": title,
            "original_path": None,
            "translated_path": None,
            "translation_task_id": None,
            "rag_status": "unavailable",
            "reading_status": "unread",
            "cited": False,
            "metadata": metadata,
            "created_at": now,
            "updated_at": now,
        }

    def _merge_record(
        self,
        source: Mapping[str, Any],
        record: PaperRecord,
        search_event: Mapping[str, Any],
    ) -> dict[str, Any]:
        updated = deepcopy(dict(source))
        metadata = deepcopy(updated.get("metadata"))
        if not isinstance(metadata, dict):
            metadata = {}
        raw_literature = metadata.get("literature")
        literature = deepcopy(raw_literature) if isinstance(raw_literature, dict) else {}
        raw_records = literature.get("records")
        records = deepcopy(raw_records) if isinstance(raw_records, dict) else {}

        existing_payload = records.get(record.paper_id)
        existing_record: PaperRecord | None = None
        if existing_payload is not None:
            try:
                existing_record = PaperRecord.model_validate(existing_payload)
            except (TypeError, ValueError) as exc:
                raise LiteratureServiceError(
                    LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                    "项目文献包含无效的规范化记录",
                    details={"source_id": source.get("id"), "paper_id": record.paper_id},
                ) from exc
        effective_record = (
            _merge_record_refresh(existing_record, record)
            if existing_record is not None
            else record
        )
        if (
            existing_record is None
            or existing_record.metadata_snapshot_hash != effective_record.metadata_snapshot_hash
        ):
            records[record.paper_id] = effective_record.model_dump(mode="json")

        raw_snapshot_ids = literature.get("search_snapshot_ids")
        snapshot_ids = (
            [str(item) for item in raw_snapshot_ids if isinstance(item, str)]
            if isinstance(raw_snapshot_ids, list)
            else []
        )
        result_snapshot_id = str(search_event["result_snapshot_id"])
        if result_snapshot_id not in snapshot_ids:
            snapshot_ids.append(result_snapshot_id)
            snapshot_ids = snapshot_ids[-_MAX_SNAPSHOT_IDS_PER_SOURCE:]

        raw_search_events = literature.get("search_events")
        search_events = (
            [deepcopy(item) for item in raw_search_events if isinstance(item, dict)]
            if isinstance(raw_search_events, list)
            else []
        )
        normalized_event = deepcopy(dict(search_event))
        if normalized_event not in search_events:
            search_events.append(normalized_event)
            search_events = search_events[-_MAX_SNAPSHOT_IDS_PER_SOURCE:]

        source_id = str(source["id"])
        raw_fulltext = literature.get("fulltext")
        if raw_fulltext:
            existing_fulltext = FullTextArtifact.model_validate(raw_fulltext)
            literature["fulltext"] = _merge_discovered_fulltext(
                existing_fulltext,
                record=effective_record,
            ).model_dump(mode="json")
        else:
            literature["fulltext"] = _initial_fulltext(source_id, record).model_dump(mode="json")
        literature.update(
            {
                "schema_version": _SOURCE_SCHEMA_VERSION,
                "primary_paper_id": literature.get("primary_paper_id") or record.paper_id,
                "records": records,
                "search_snapshot_ids": snapshot_ids,
                "search_events": search_events,
            }
        )
        metadata["literature"] = literature
        if "authors" not in metadata:
            metadata["authors"] = list(record.authors)
        if "year" not in metadata:
            metadata["year"] = str(record.year) if record.year is not None else None
        if "journal" not in metadata:
            metadata["journal"] = record.venue
        if "tags" not in metadata:
            metadata["tags"] = []
        updated["metadata"] = metadata

        if updated == source:
            return dict(source)
        updated["updated_at"] = self._now()
        return updated

    def _now_datetime(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_factory must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return self._now_datetime().isoformat()


__all__ = [
    "IdentityKind",
    "ImportDisposition",
    "LiteratureImportBatch",
    "LiteratureImportItem",
    "LiteratureSearchExecution",
    "LiteratureService",
    "LiteratureServiceError",
    "LiteratureServiceErrorCode",
    "ProjectSourceStore",
]
