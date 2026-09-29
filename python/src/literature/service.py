"""Application service for literature discovery and project import.

The service owns provider selection, bounded search executions, conservative
identity matching, and one-write project imports.  It deliberately does not
download, parse, index, or answer from full text; those are later PoC phases.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import mimetypes
import re
import unicodedata
import uuid
from asyncio import Lock
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.literature.answer import (
    AnswerEvidenceCandidate,
    AnswerResponseError,
    AnswerStatus,
    InsufficientReason,
    RejectedClaim,
    RejectedClaimReason,
    UnresolvedEvidence,
    build_answer_prompt,
    build_answer_system_prompt,
    parse_answer_response,
    validate_answer_claims,
)
from src.literature.answer_model import EvidenceAnswerModel
from src.literature.evidence import (
    CHUNKER_VERSION,
    INDEX_VERSION,
    PARSER_VERSION,
    EvidenceResolutionError,
    assert_current_versions,
    evidence_metadata,
    normalized_pages,
    resolve_evidence_span,
    sha256_file,
)
from src.literature.fulltext import (
    FullTextDownloader,
    FullTextDownloadError,
    maybe_aclose,
    select_open_pdf_location,
)
from src.literature.models import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    AnswerClaim,
    EvidenceSpan,
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
_MAX_CACHED_PAGE_TEXTS = 8
_EVIDENCE_ARTIFACT_SUFFIXES = {".pdf"}
_FILE_PRESENT_STATUSES = {
    FullTextStatus.FULLTEXT_READY,
    FullTextStatus.PARSING,
    FullTextStatus.PARSED,
    FullTextStatus.PARSE_FAILED,
    FullTextStatus.INDEXING,
    FullTextStatus.INDEXED,
    FullTextStatus.INDEX_FAILED,
}
_SAFE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_SEARCH_EXECUTION_ID_RE = re.compile(r"^search_exec_[0-9a-f]{32}$")
DEFAULT_ANSWER_TOP_K = 8
MAX_ANSWER_TOP_K = 20
MAX_ANSWER_SOURCES = 50


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

    def read_sources(self, project_path: str) -> list[dict[str, Any]]:
        """Return the project's sources without modifying them."""
        ...

    def resolve_source_artifact(self, project_path: str, source: Mapping[str, Any]) -> str:
        """Return the project-internal absolute path of a source's full text."""
        ...


@runtime_checkable
class PageIndexStore(Protocol):
    """Narrow page-aware index boundary required by the service."""

    async def embedding_identity(self) -> tuple[str, str]:
        """Return the embedding model/version the index actually uses."""
        ...

    async def index_pages(
        self,
        *,
        doc_id: str,
        title: str,
        pages: Sequence[tuple[int, str]],
        artifact_sha256: str,
        project_root: str | None = None,
        source_id: str | None = None,
        filename: str | None = None,
        force: bool = False,
    ) -> Mapping[str, Any]:
        """Write page-scoped chunks and return the stored document entry."""
        ...

    async def get_chunk(
        self, chunk_id: str, *, source_id: str | None = None
    ) -> Mapping[str, Any] | None:
        """Return one stored chunk, optionally restricted to one source."""
        ...

    async def get_document(self, doc_id: str) -> Mapping[str, Any] | None:
        """Return one stored document entry or ``None``."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Project-side storage of an acquired full-text file."""

    def store_source_artifact(
        self,
        project_path: str,
        source_id: str,
        filename: str,
        content: bytes,
    ) -> str:
        """Write one artifact inside the project and return its absolute path."""
        ...


class RetrievedChunk(BaseModel):
    """One scoped retrieval hit: a chunk identity plus the source it belongs to.

    ``source_id`` may be blank for a legacy flat-text chunk that carries no source
    identity; the service reports those as unresolved instead of dropping them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(default="", max_length=64)
    text: str = Field(default="", max_length=100_000)


@runtime_checkable
class EvidenceRetriever(Protocol):
    """Scoped page retrieval: the only route by which an answer finds evidence."""

    async def retrieve(
        self,
        *,
        project_root: str,
        source_ids: Sequence[str],
        query: str,
        top_k: int,
    ) -> Sequence[RetrievedChunk]:
        """Return page-level hits inside one project and its selected sources."""
        ...


class LiteratureServiceErrorCode(StrEnum):
    UNKNOWN_PROVIDER = "unknown_provider"
    SEARCH_EXECUTION_NOT_FOUND = "search_execution_not_found"
    RECORD_NOT_IN_SNAPSHOT = "record_not_in_snapshot"
    IDENTITY_CONFLICT = "identity_conflict"
    PROJECT_DATA_INVALID = "project_data_invalid"
    SOURCE_ID_EXHAUSTED = "source_id_exhausted"
    SEARCH_EXECUTION_ID_EXHAUSTED = "search_execution_id_exhausted"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_NOT_LITERATURE = "source_not_literature"
    SOURCE_ARTIFACT_MISSING = "source_artifact_missing"
    UNSUPPORTED_EVIDENCE_FORMAT = "unsupported_evidence_format"
    ACCESS_UNAVAILABLE = "access_unavailable"
    ACQUIRE_FAILED = "acquire_failed"
    FULLTEXT_ALREADY_PRESENT = "fulltext_already_present"
    ARTIFACT_STORE_UNAVAILABLE = "artifact_store_unavailable"
    DOWNLOADER_UNAVAILABLE = "downloader_unavailable"
    PARSE_FAILED = "parse_failed"
    INDEX_FAILED = "index_failed"
    INDEX_STORE_UNAVAILABLE = "index_store_unavailable"
    CHUNK_NOT_FOUND = "chunk_not_found"
    EVIDENCE_UNRESOLVED = "evidence_unresolved"
    SCOPE_REQUIRED = "scope_required"
    ANSWER_REQUEST_INVALID = "answer_request_invalid"
    ANSWER_MODEL_UNAVAILABLE = "answer_model_unavailable"
    ANSWER_GENERATION_FAILED = "answer_generation_failed"
    ANSWER_INVALID_RESPONSE = "answer_invalid_response"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


#: Failures that mean "the answer service cannot run right now", not "the corpus
#: is silent"; they must not be folded into an honest insufficiency.
_UNAVAILABLE_EVIDENCE_CODES = frozenset(
    {
        LiteratureServiceErrorCode.INDEX_STORE_UNAVAILABLE,
        LiteratureServiceErrorCode.RETRIEVAL_UNAVAILABLE,
    }
)


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


class LiteratureIndexResult(BaseModel):
    """Page-aware indexing outcome for one project literature source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    doc_id: str = Field(min_length=1, max_length=200)
    status: FullTextStatus
    reused: bool
    chunk_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    artifact_sha256: str
    index_fingerprint: str
    parser_version: str
    chunker_version: str
    embedding_model: str
    embedding_version: str
    index_version: str
    indexed_at: datetime


class LiteratureEvidenceResult(BaseModel):
    """A retrieved chunk resolved back to a verifiable page quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    doc_id: str = Field(min_length=1, max_length=200)
    title: str
    chunk_id: str = Field(min_length=1, max_length=128)
    paper_id: str | None = None
    span: EvidenceSpan


class LiteratureFullTextResult(BaseModel):
    """Outcome of one open-access full-text acquisition attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    status: FullTextStatus
    reused: bool
    local_path: str | None = None
    source_url: str | None = None
    sha256: str | None = None
    file_size_bytes: int | None = None
    mime_type: str | None = None
    acquired_at: datetime | None = None
    failure_reason: str | None = None


class LiteratureAnswerResult(BaseModel):
    """One evidence-grounded answer inside a project and source scope (P3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    project_root: str
    source_ids: list[str]
    status: AnswerStatus
    insufficient_reason: InsufficientReason | None = None
    claims: list[AnswerClaim]
    evidence: list[LiteratureEvidenceResult]
    rejected_claims: list[RejectedClaim]
    unresolved: list[UnresolvedEvidence]
    retrieved_chunk_count: int = Field(ge=0)
    model_provider: str
    model_name: str
    model_config_hash: str
    generated_at: datetime

    @model_validator(mode="after")
    def validate_result_scope(self) -> LiteratureAnswerResult:
        evidence_ids = {item.span.evidence_id for item in self.evidence}
        for claim in self.claims:
            if not set(claim.evidence_ids) <= evidence_ids:
                raise ValueError("a claim cites evidence that is missing from the result")
        requested = set(self.source_ids)
        for item in self.evidence:
            if item.source_id not in requested:
                raise ValueError("evidence falls outside the requested source scope")
        if self.status is AnswerStatus.ANSWERED and not self.claims:
            raise ValueError("an answered result must carry at least one claim")
        if self.status is AnswerStatus.INSUFFICIENT and self.claims:
            raise ValueError("an insufficient result cannot carry claims")
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
        index_store: PageIndexStore | None = None,
        downloader: FullTextDownloader | None = None,
        artifact_store: ArtifactStore | None = None,
        retriever: EvidenceRetriever | None = None,
        answer_model: EvidenceAnswerModel | None = None,
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
        if index_store is not None and not isinstance(index_store, PageIndexStore):
            raise TypeError("index_store does not satisfy PageIndexStore")
        if retriever is not None and not isinstance(retriever, EvidenceRetriever):
            raise TypeError("retriever does not satisfy EvidenceRetriever")
        if answer_model is not None and not isinstance(answer_model, EvidenceAnswerModel):
            raise TypeError("answer_model does not satisfy EvidenceAnswerModel")

        self._providers = registry
        self._project_store = project_store
        self._index_store = index_store
        self._downloader = downloader
        self._retriever = retriever
        self._answer_model = answer_model
        self._artifact_store = (
            artifact_store
            if artifact_store is not None
            else (project_store if isinstance(project_store, ArtifactStore) else None)
        )
        self._execution_capacity = snapshot_capacity
        self._search_executions: OrderedDict[str, LiteratureSearchExecution] = OrderedDict()
        self._page_text_cache: OrderedDict[str, dict[int, str]] = OrderedDict()
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

    async def acquire_fulltext(
        self,
        *,
        project_path: str,
        source_id: str,
        force: bool = False,
    ) -> LiteratureFullTextResult:
        """Acquire the provider-declared open PDF for one project literature source.

        Only locations the provider itself marked ``open`` are used.  The plan's
        state machine is driven explicitly (``metadata_only → acquiring →
        fulltext_ready`` or a failure state), and an existing artifact is never
        silently replaced: ``force`` is required to fetch again.
        """

        source = self._require_literature_source(project_path, source_id)
        record = self._primary_record(source)
        if record is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                "项目文献缺少规范化记录，无法确定开放全文位置",
                details={"source_id": source_id},
            )
        downloader = self._require_downloader()
        artifact_store = self._require_artifact_store()

        existing = self._existing_fulltext(project_path, source_id)
        existing_status = FullTextStatus(existing["status"]) if existing else None
        if (
            not force
            and existing_status in _FILE_PRESENT_STATUSES
            and existing is not None
            and existing.get("local_path")
        ):
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.FULLTEXT_ALREADY_PRESENT,
                "该文献已有全文附件；如需重新获取请显式使用 force",
                details={"source_id": source_id, "status": existing_status.value},
            )

        location = select_open_pdf_location(record)
        if location is None:
            artifact = FullTextArtifact.model_validate(
                {
                    **(dict(existing) if existing else {}),
                    "source_id": source_id,
                    "status": FullTextStatus.ACCESS_UNAVAILABLE,
                    "failure_reason": "该记录没有声明可用的开放 PDF 位置",
                    "local_path": None,
                    "sha256": None,
                    "file_size_bytes": None,
                    "mime_type": None,
                    "acquired_at": None,
                    "artifact_id": None,
                }
            )
            self._write_fulltext(project_path, source_id, artifact)
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ACCESS_UNAVAILABLE,
                "该记录没有声明可用的开放 PDF 位置",
                details={"source_id": source_id},
            )

        self._write_fulltext(
            project_path,
            source_id,
            FullTextArtifact.model_validate(
                {
                    "source_id": source_id,
                    "status": FullTextStatus.ACQUIRING,
                    "source_url": location.url,
                    "access_status": location.access_status,
                }
            ),
        )

        acquired_at = self._now_datetime()
        try:
            downloaded = await downloader.download(location.url)
        except FullTextDownloadError as exc:
            artifact = self._acquire_failure_artifact(
                project_path=project_path,
                source_id=source_id,
                failure_reason=f"获取开放全文失败：{exc.code.value}",
                source_url=location.url,
                access_status=location.access_status,
            )
            self._write_fulltext(project_path, source_id, artifact)
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ACQUIRE_FAILED,
                "获取开放全文失败",
                details={"source_id": source_id, "code": exc.code.value, **exc.details},
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive, downloader is pluggable
            artifact = self._acquire_failure_artifact(
                project_path=project_path,
                source_id=source_id,
                failure_reason=f"获取开放全文失败：{type(exc).__name__}",
                source_url=location.url,
                access_status=location.access_status,
            )
            self._write_fulltext(project_path, source_id, artifact)
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ACQUIRE_FAILED,
                "获取开放全文失败",
                details={"source_id": source_id, "exception_type": type(exc).__name__},
            ) from exc

        filename = self._artifact_filename_for(record, downloaded.source_url)
        # D-023 rule 4: the recorded hash is the hash of the bytes that are about
        # to be written, never the downloader's self-report alone.
        actual_sha256 = hashlib.sha256(downloaded.content).hexdigest()
        if downloaded.sha256 != actual_sha256:
            mismatch_artifact = self._acquire_failure_artifact(
                project_path=project_path,
                source_id=source_id,
                failure_reason="获取开放全文失败：downloader_hash_mismatch",
                source_url=location.url,
                access_status=location.access_status,
            )
            self._write_fulltext(project_path, source_id, mismatch_artifact)
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ACQUIRE_FAILED,
                "下载器报告的哈希与实际字节不一致",
                details={"source_id": source_id, "code": "downloader_hash_mismatch"},
            )
        try:
            local_path = artifact_store.store_source_artifact(
                project_path,
                source_id,
                filename,
                downloaded.content,
            )
        except LiteratureServiceError:
            raise
        except Exception as exc:
            artifact = self._acquire_failure_artifact(
                project_path=project_path,
                source_id=source_id,
                failure_reason=f"写入全文附件失败：{type(exc).__name__}",
                source_url=location.url,
                access_status=location.access_status,
            )
            self._write_fulltext(project_path, source_id, artifact)
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ACQUIRE_FAILED,
                "写入全文附件失败",
                details={"source_id": source_id, "exception_type": type(exc).__name__},
            ) from exc

        artifact = FullTextArtifact.model_validate(
            {
                "source_id": source_id,
                "status": FullTextStatus.FULLTEXT_READY,
                "local_path": local_path,
                "source_url": downloaded.source_url,
                "access_status": location.access_status,
                "acquired_at": acquired_at,
                "file_size_bytes": downloaded.size_bytes,
                "mime_type": downloaded.mime_type,
                "sha256": actual_sha256,
            }
        )
        self._write_source_path(project_path, source_id, local_path)
        self._write_fulltext(project_path, source_id, artifact)

        return LiteratureFullTextResult(
            source_id=source_id,
            status=artifact.status,
            reused=False,
            local_path=artifact.local_path,
            source_url=artifact.source_url,
            sha256=artifact.sha256,
            file_size_bytes=artifact.file_size_bytes,
            mime_type=artifact.mime_type,
            acquired_at=artifact.acquired_at,
        )

    def _acquire_failure_artifact(
        self,
        *,
        project_path: str,
        source_id: str,
        failure_reason: str,
        source_url: str,
        access_status: AccessStatus,
    ) -> FullTextArtifact:
        existing = self._existing_fulltext(project_path, source_id)
        payload: dict[str, Any] = {
            "source_id": source_id,
            "status": FullTextStatus.ACQUIRE_FAILED,
            "failure_reason": failure_reason,
            "source_url": source_url,
            "access_status": access_status,
        }
        if existing is not None and existing.get("artifact_version"):
            payload["artifact_version"] = int(existing["artifact_version"])
        return FullTextArtifact.model_validate(payload)

    def _write_source_path(self, project_path: str, source_id: str, local_path: str) -> None:
        """Point the project source at its newly acquired artifact."""

        def update(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
            updated = deepcopy(sources)
            for item in updated:
                if str(item.get("id")) != source_id:
                    continue
                item["original_path"] = local_path
                return updated
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_NOT_FOUND,
                "项目文献不存在",
                details={"source_id": source_id},
            )

        self._project_store.update_sources(project_path, update)

    def _require_downloader(self) -> FullTextDownloader:
        if self._downloader is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.DOWNLOADER_UNAVAILABLE,
                "未配置开放全文下载器",
            )
        return self._downloader

    def _require_artifact_store(self) -> ArtifactStore:
        if self._artifact_store is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ARTIFACT_STORE_UNAVAILABLE,
                "项目存储不支持写入全文附件",
            )
        return self._artifact_store

    def _primary_record(self, source: Mapping[str, Any]) -> PaperRecord | None:
        metadata = source.get("metadata")
        literature = metadata.get("literature") if isinstance(metadata, Mapping) else None
        if not isinstance(literature, Mapping):
            return None
        paper_id = literature.get("primary_paper_id")
        records = literature.get("records")
        if not isinstance(paper_id, str) or not isinstance(records, Mapping):
            return None
        payload = records.get(paper_id)
        if not isinstance(payload, Mapping):
            return None
        try:
            return PaperRecord.model_validate(payload)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _artifact_filename_for(record: PaperRecord, source_url: str) -> str:
        if record.external_ids.arxiv:
            stem = record.external_ids.arxiv.replace("/", "-")
            if record.external_ids.arxiv_version is not None:
                stem = f"{stem}v{record.external_ids.arxiv_version}"
            return f"{stem}.pdf"
        url_name = Path(urlparse(source_url).path).name
        if url_name.lower().endswith(".pdf"):
            return url_name
        return f"{record.paper_id}.pdf"

    async def index_source(
        self,
        *,
        project_path: str,
        source_id: str,
        force: bool = False,
    ) -> LiteratureIndexResult:
        """Parse one project literature source by page and build its evidence index.

        Only PDF attachments are evidence-indexable in the first version: the
        other extractors synthesize a single ``page_num=1`` and cannot support an
        honest page coordinate.  Every state transition of the plan's full-text
        state machine is persisted so a failure stays visible.
        """

        index_store = self._require_index_store()
        source = self._require_literature_source(project_path, source_id)
        artifact_path = Path(self._project_store.resolve_source_artifact(project_path, source))
        if artifact_path.suffix.lower() not in _EVIDENCE_ARTIFACT_SUFFIXES:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.UNSUPPORTED_EVIDENCE_FORMAT,
                "首版只对 PDF 附件建立页码级证据索引：该附件没有可核验的真实页码",
                details={"source_id": source_id, "suffix": artifact_path.suffix.lower()},
            )
        try:
            file_size = artifact_path.stat().st_size
        except OSError as exc:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_ARTIFACT_MISSING,
                "文献附件不存在或不可读取",
                details={"source_id": source_id},
            ) from exc
        if file_size <= 0:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_ARTIFACT_MISSING,
                "文献附件为空",
                details={"source_id": source_id},
            )

        artifact_sha256 = await asyncio.to_thread(sha256_file, artifact_path)
        mime_type = mimetypes.guess_type(artifact_path.name)[0] or "application/pdf"
        acquired_at = self._now_datetime()
        doc_id = f"project:{source_id}"

        def artifact(
            status: FullTextStatus, *, failure_reason: str | None = None
        ) -> FullTextArtifact:
            return self._build_file_artifact(
                project_path=project_path,
                source_id=source_id,
                status=status,
                local_path=str(artifact_path),
                artifact_sha256=artifact_sha256,
                file_size_bytes=file_size,
                mime_type=mime_type,
                acquired_at=acquired_at,
                failure_reason=failure_reason,
            )

        self._write_fulltext(project_path, source_id, artifact(FullTextStatus.PARSING))
        try:
            pages = await asyncio.to_thread(self._parse_pages, artifact_path)
        except Exception as exc:
            reason = f"解析失败：{type(exc).__name__}"
            self._write_fulltext(
                project_path,
                source_id,
                artifact(FullTextStatus.PARSE_FAILED, failure_reason=reason),
            )
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.PARSE_FAILED,
                "无法解析该 PDF 的页面文本",
                details={"source_id": source_id, "exception_type": type(exc).__name__},
            ) from exc
        if not any(text.strip() for _, text in pages):
            reason = "PDF 没有可提取文本（疑似扫描版）"
            self._write_fulltext(
                project_path,
                source_id,
                artifact(FullTextStatus.PARSE_FAILED, failure_reason=reason),
            )
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.PARSE_FAILED,
                reason,
                details={"source_id": source_id},
            )
        self._write_fulltext(project_path, source_id, artifact(FullTextStatus.PARSED))

        embedding_model, embedding_version = await index_store.embedding_identity()
        self._write_fulltext(project_path, source_id, artifact(FullTextStatus.INDEXING))
        try:
            entry = await index_store.index_pages(
                doc_id=doc_id,
                title=str(source.get("title") or source_id),
                pages=pages,
                artifact_sha256=artifact_sha256,
                project_root=project_path,
                source_id=source_id,
                filename=self._artifact_filename(source),
                force=force,
            )
        except LiteratureServiceError:
            self._write_fulltext(
                project_path,
                source_id,
                artifact(FullTextStatus.INDEX_FAILED, failure_reason="索引写入失败"),
            )
            raise
        except Exception as exc:
            self._write_fulltext(
                project_path,
                source_id,
                artifact(FullTextStatus.INDEX_FAILED, failure_reason="索引写入失败"),
            )
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.INDEX_FAILED,
                "建立页级索引失败",
                details={"source_id": source_id, "exception_type": type(exc).__name__},
            ) from exc

        indexed_at = self._now_datetime()
        index_payload = {
            "doc_id": doc_id,
            "chunk_count": int(entry.get("chunk_count") or 0),
            "page_count": int(entry.get("page_count") or 0),
            "artifact_sha256": artifact_sha256,
            "index_fingerprint": str(entry.get("index_fingerprint") or ""),
            "parser_version": str(entry.get("parser_version") or PARSER_VERSION),
            "chunker_version": str(entry.get("chunker_version") or CHUNKER_VERSION),
            "embedding_model": str(entry.get("embedding_model") or embedding_model),
            "embedding_version": str(entry.get("embedding_version") or embedding_version),
            "index_version": str(entry.get("index_version") or INDEX_VERSION),
            "indexed_at": str(entry.get("indexed_at") or indexed_at.isoformat()),
        }
        self._write_fulltext(
            project_path,
            source_id,
            artifact(FullTextStatus.INDEXED),
            index=index_payload,
        )
        return LiteratureIndexResult(
            source_id=source_id,
            doc_id=doc_id,
            status=FullTextStatus.INDEXED,
            reused=bool(entry.get("reused")),
            chunk_count=index_payload["chunk_count"],
            page_count=index_payload["page_count"],
            artifact_sha256=artifact_sha256,
            index_fingerprint=index_payload["index_fingerprint"],
            parser_version=index_payload["parser_version"],
            chunker_version=index_payload["chunker_version"],
            embedding_model=index_payload["embedding_model"],
            embedding_version=index_payload["embedding_version"],
            index_version=index_payload["index_version"],
            indexed_at=indexed_at,
        )

    async def resolve_evidence(
        self,
        *,
        project_path: str,
        source_id: str,
        chunk_id: str,
    ) -> LiteratureEvidenceResult:
        """Resolve one indexed chunk back to a verifiable page quote.

        The chunk is looked up in the server-owned index (never supplied by the
        caller), then the artifact is re-read, re-hashed and re-parsed, so a
        replaced PDF, a stale index or a version change fails explicitly instead
        of producing a plausible-looking citation.
        """

        index_store = self._require_index_store()
        source = self._require_literature_source(project_path, source_id)
        chunk = await index_store.get_chunk(chunk_id, source_id=source_id)
        if chunk is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.CHUNK_NOT_FOUND,
                "检索块不存在或索引已重建，请重新检索",
                details={"source_id": source_id, "chunk_id": chunk_id},
            )
        metadata = dict(chunk.get("metadata") or {})

        # Sufficiency first: a legacy flat-text or translation chunk carries no
        # page metadata at all, and that is the most precise diagnosis for it.
        embedding_model, embedding_version = await index_store.embedding_identity()
        try:
            fields = evidence_metadata(metadata)
            assert_current_versions(
                metadata,
                embedding_model=embedding_model,
                embedding_version=embedding_version,
            )
        except EvidenceResolutionError as exc:
            raise self._evidence_error(exc, source_id=source_id, chunk_id=chunk_id) from exc

        if str(metadata.get("source_id") or "") != source_id:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED,
                "检索块不属于该项目文献",
                details={"source_id": source_id, "chunk_id": chunk_id},
            )

        artifact_path = Path(self._project_store.resolve_source_artifact(project_path, source))
        artifact_sha256 = await asyncio.to_thread(sha256_file, artifact_path)
        if artifact_sha256 != fields["artifact_sha256"]:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED,
                "当前附件与索引记录的文件哈希不一致，需要重新建立索引",
                details={
                    "source_id": source_id,
                    "chunk_id": chunk_id,
                    "code": "artifact_hash_mismatch",
                },
            )

        page_texts = await self._page_texts(artifact_path, artifact_sha256)
        try:
            span = resolve_evidence_span(
                source_id=source_id,
                artifact_sha256=artifact_sha256,
                page_texts=page_texts,
                page_number=fields["page_start"],
                char_start=fields["char_start"],
                char_end=fields["char_end"],
                chunk_id=str(chunk.get("chunk_id") or chunk_id),
                parser_version=fields["parser_version"],
                chunker_version=fields["chunker_version"],
                embedding_model=fields["embedding_model"],
                embedding_version=fields["embedding_version"],
                index_version=fields["index_version"],
            )
        except EvidenceResolutionError as exc:
            raise self._evidence_error(exc, source_id=source_id, chunk_id=chunk_id) from exc
        if span.exact_quote != str(chunk.get("text") or ""):
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED,
                "索引中的块文本与哈希文档的对应页不一致，需要重新建立索引",
                details={"source_id": source_id, "chunk_id": chunk_id, "code": "quote_mismatch"},
            )

        return LiteratureEvidenceResult(
            source_id=source_id,
            doc_id=str(metadata.get("doc_id") or f"project:{source_id}"),
            title=str(source.get("title") or source_id),
            chunk_id=span.chunk_id,
            paper_id=self._primary_paper_id(source),
            span=span,
        )

    async def answer_question(
        self,
        *,
        project_path: str,
        question: str,
        source_ids: Sequence[str],
        top_k: int = DEFAULT_ANSWER_TOP_K,
    ) -> LiteratureAnswerResult:
        """Answer a question from evidence scoped to one project and its sources.

        The order of operations is part of the contract: scope is validated first,
        every retrieval hit is resolved back to a real page quote, and the model is
        only called when at least one verifiable piece of evidence exists.  When
        there is no evidence the result says so without consulting a model.
        """

        normalized_question = question.strip() if isinstance(question, str) else ""
        if not normalized_question:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID,
                "研究问题不能为空",
            )
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= MAX_ANSWER_TOP_K
        ):
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID,
                f"top_k 必须是 1 到 {MAX_ANSWER_TOP_K} 之间的整数",
                details={"top_k": top_k},
            )

        scoped_source_ids: list[str] = []
        for raw_source_id in source_ids or []:
            normalized_source_id = str(raw_source_id).strip()
            if normalized_source_id and normalized_source_id not in scoped_source_ids:
                scoped_source_ids.append(normalized_source_id)
        if not scoped_source_ids:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SCOPE_REQUIRED,
                "多文献证据问答必须显式选择至少一篇项目文献",
            )
        if len(scoped_source_ids) > MAX_ANSWER_SOURCES:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID,
                f"单次问答最多选择 {MAX_ANSWER_SOURCES} 篇文献",
                details={"source_count": len(scoped_source_ids)},
            )
        for source_id in scoped_source_ids:
            self._require_literature_source(project_path, source_id)

        retriever = self._require_retriever()
        answer_model = self._require_answer_model()
        generated_at = self._now_datetime()

        hits = await retriever.retrieve(
            project_root=project_path,
            source_ids=list(scoped_source_ids),
            query=normalized_question,
            top_k=top_k,
        )
        unique_hits: list[RetrievedChunk] = []
        seen_chunk_ids: set[str] = set()
        for hit in hits:
            if hit.chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(hit.chunk_id)
            unique_hits.append(hit)
        if len(unique_hits) > MAX_ANSWER_TOP_K:
            # Silently trimming the evidence set could drop the only verifiable
            # quote, so a retriever that ignores top_k fails loudly instead.
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID,
                f"检索返回的去重命中数超过上限 {MAX_ANSWER_TOP_K}",
                details={
                    "reason": "too_many_retrieval_hits",
                    "hit_count": len(unique_hits),
                },
            )

        candidates: dict[str, AnswerEvidenceCandidate] = {}
        resolved: dict[str, LiteratureEvidenceResult] = {}
        unresolved: list[UnresolvedEvidence] = []
        for hit in unique_hits:
            if not hit.source_id:
                unresolved.append(
                    UnresolvedEvidence(
                        source_id="",
                        chunk_id=hit.chunk_id,
                        reason="missing_source_scope",
                        detail="检索块没有来源标识，无法限定在所选文献范围内",
                    )
                )
                continue
            if hit.source_id not in scoped_source_ids:
                unresolved.append(
                    UnresolvedEvidence(
                        source_id=hit.source_id,
                        chunk_id=hit.chunk_id,
                        reason="out_of_scope_chunk",
                        detail="检索块不属于本次选定的文献范围",
                    )
                )
                continue
            try:
                evidence = await self.resolve_evidence(
                    project_path=project_path,
                    source_id=hit.source_id,
                    chunk_id=hit.chunk_id,
                )
            except LiteratureServiceError as exc:
                # A broken store is not "the corpus cannot answer": it must stay
                # distinguishable from an honest insufficiency (plan 5.9 rule 7).
                if exc.code in _UNAVAILABLE_EVIDENCE_CODES:
                    raise
                unresolved.append(
                    UnresolvedEvidence(
                        source_id=hit.source_id,
                        chunk_id=hit.chunk_id,
                        reason=str(exc.details.get("code") or exc.code.value),
                        detail=str(exc),
                    )
                )
                continue
            span = evidence.span
            candidates[span.evidence_id] = AnswerEvidenceCandidate(
                evidence_id=span.evidence_id,
                source_id=evidence.source_id,
                title=evidence.title,
                page_start=span.page_start,
                page_end=span.page_end,
                chunk_id=span.chunk_id,
                exact_quote=span.exact_quote,
                context_before=span.context_before,
                context_after=span.context_after,
            )
            resolved[span.evidence_id] = evidence

        envelope: dict[str, Any] = {
            "question": normalized_question,
            "project_root": project_path,
            "source_ids": list(scoped_source_ids),
            "model_provider": answer_model.identity.provider,
            "model_name": answer_model.identity.model,
            "model_config_hash": answer_model.identity.config_hash,
            "generated_at": generated_at,
            "retrieved_chunk_count": len(unique_hits),
            "unresolved": unresolved,
        }

        if not unique_hits:
            return LiteratureAnswerResult(
                **envelope,
                status=AnswerStatus.INSUFFICIENT,
                insufficient_reason=InsufficientReason.NO_RETRIEVAL_HITS,
                claims=[],
                evidence=[],
                rejected_claims=[],
            )
        if not candidates:
            return LiteratureAnswerResult(
                **envelope,
                status=AnswerStatus.INSUFFICIENT,
                insufficient_reason=InsufficientReason.NO_RESOLVABLE_EVIDENCE,
                claims=[],
                evidence=[],
                rejected_claims=[],
            )

        try:
            prompt = build_answer_prompt(
                question=normalized_question,
                candidates=list(candidates.values()),
            )
        except ValueError as exc:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_REQUEST_INVALID,
                "证据集合超出提示预算",
                details={
                    "reason": str(exc),
                    "candidate_count": len(candidates),
                    "question_chars": len(normalized_question),
                },
            ) from exc
        try:
            raw_response = await answer_model.complete(
                system_prompt=build_answer_system_prompt(),
                prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001 - every provider failure is explicit
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_GENERATION_FAILED,
                "证据问答模型调用失败",
                details={
                    "exception_type": type(exc).__name__,
                    "source_ids": list(scoped_source_ids),
                },
            ) from exc

        try:
            parsed = parse_answer_response(raw_response)
        except AnswerResponseError as exc:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_INVALID_RESPONSE,
                "证据问答模型未返回约定的结构化结果",
                details={"reason": str(exc)},
            ) from exc

        claims, rejected_claims = validate_answer_claims(
            parsed=parsed,
            candidates=candidates,
            identity=answer_model.identity,
            generated_at=generated_at,
        )

        cited_ids: list[str] = []
        for claim in claims:
            for evidence_id in claim.evidence_ids:
                if evidence_id not in cited_ids:
                    cited_ids.append(evidence_id)

        if claims:
            return LiteratureAnswerResult(
                **envelope,
                status=AnswerStatus.ANSWERED,
                insufficient_reason=None,
                claims=claims,
                evidence=[resolved[evidence_id] for evidence_id in cited_ids],
                rejected_claims=rejected_claims,
            )

        declined = bool(rejected_claims) and all(
            item.reason is RejectedClaimReason.MODEL_REPORTED_INSUFFICIENT
            for item in rejected_claims
        )
        return LiteratureAnswerResult(
            **envelope,
            status=AnswerStatus.INSUFFICIENT,
            insufficient_reason=(
                InsufficientReason.MODEL_REPORTED_INSUFFICIENT
                if not parsed or declined
                else InsufficientReason.ALL_CLAIMS_REJECTED
            ),
            claims=[],
            evidence=[],
            rejected_claims=rejected_claims,
        )

    async def aclose(self) -> None:
        failures: list[Exception] = []
        seen: set[int] = set()
        if self._downloader is not None:
            try:
                await maybe_aclose(self._downloader)
            except Exception as exc:  # pragma: no cover - lifecycle caller logs failures
                failures.append(exc)
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

    def _require_index_store(self) -> PageIndexStore:
        if self._index_store is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.INDEX_STORE_UNAVAILABLE,
                "页级索引未启用",
            )
        return self._index_store

    def _require_retriever(self) -> EvidenceRetriever:
        if self._retriever is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.RETRIEVAL_UNAVAILABLE,
                "范围内检索未启用",
            )
        return self._retriever

    def _require_answer_model(self) -> EvidenceAnswerModel:
        if self._answer_model is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.ANSWER_MODEL_UNAVAILABLE,
                "证据问答模型未配置",
            )
        return self._answer_model

    def _require_literature_source(self, project_path: str, source_id: str) -> Mapping[str, Any]:
        sources = self._project_store.read_sources(project_path)
        source = next((item for item in sources if str(item.get("id")) == source_id), None)
        if source is None:
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_NOT_FOUND,
                "项目文献不存在",
                details={"source_id": source_id},
            )
        self._validate_sources([source])
        metadata = source.get("metadata")
        literature = metadata.get("literature") if isinstance(metadata, Mapping) else None
        if not isinstance(literature, Mapping):
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_NOT_LITERATURE,
                "该文献不是通过公开检索入库的文献条目，暂无页级证据状态",
                details={"source_id": source_id},
            )
        return source

    def _build_file_artifact(
        self,
        *,
        project_path: str,
        source_id: str,
        status: FullTextStatus,
        local_path: str,
        artifact_sha256: str,
        file_size_bytes: int,
        mime_type: str,
        acquired_at: datetime,
        failure_reason: str | None = None,
    ) -> FullTextArtifact:
        existing = self._existing_fulltext(project_path, source_id)
        payload: dict[str, Any] = {
            "source_id": source_id,
            "status": status,
            "local_path": local_path,
            "sha256": artifact_sha256,
            "file_size_bytes": file_size_bytes,
            "mime_type": mime_type,
            "acquired_at": acquired_at,
            "failure_reason": failure_reason,
        }
        if existing is not None:
            if existing.get("source_url"):
                payload["source_url"] = existing["source_url"]
            if existing.get("access_status"):
                payload["access_status"] = existing["access_status"]
            payload["artifact_version"] = int(existing.get("artifact_version") or 1)
        return FullTextArtifact.model_validate(payload)

    def _existing_fulltext(self, project_path: str, source_id: str) -> Mapping[str, Any] | None:
        for source in self._project_store.read_sources(project_path):
            if str(source.get("id")) != source_id:
                continue
            metadata = source.get("metadata")
            literature = metadata.get("literature") if isinstance(metadata, Mapping) else None
            if isinstance(literature, Mapping):
                fulltext = literature.get("fulltext")
                return fulltext if isinstance(fulltext, Mapping) else None
            return None
        return None

    def _write_fulltext(
        self,
        project_path: str,
        source_id: str,
        artifact: FullTextArtifact,
        *,
        index: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist one full-text state transition for a project literature source."""

        def update(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
            updated = deepcopy(sources)
            for item in updated:
                if str(item.get("id")) != source_id:
                    continue
                metadata = item.get("metadata")
                if not isinstance(metadata, dict):
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.PROJECT_DATA_INVALID,
                        "项目文献缺少 metadata 对象",
                        details={"source_id": source_id},
                    )
                literature = metadata.get("literature")
                if not isinstance(literature, dict):
                    raise LiteratureServiceError(
                        LiteratureServiceErrorCode.SOURCE_NOT_LITERATURE,
                        "该文献不是通过公开检索入库的文献条目，暂无页级证据状态",
                        details={"source_id": source_id},
                    )
                literature["fulltext"] = artifact.model_dump(mode="json")
                if index is not None:
                    literature["index"] = dict(index)
                return updated
            raise LiteratureServiceError(
                LiteratureServiceErrorCode.SOURCE_NOT_FOUND,
                "项目文献不存在",
                details={"source_id": source_id},
            )

        self._project_store.update_sources(project_path, update)

    @staticmethod
    def _parse_pages(path: Path) -> list[tuple[int, str]]:
        from src.parser import extract_document

        document = extract_document(path)
        return [(page.page_num, page.text) for page in document.pages]

    async def _page_texts(self, path: Path, artifact_sha256: str) -> dict[int, str]:
        """Normalized page text of one artifact, cached by hash and parser version."""

        key = f"{artifact_sha256}:{PARSER_VERSION}"
        cached = self._page_text_cache.get(key)
        if cached is not None:
            self._page_text_cache.move_to_end(key)
            return cached
        pages = await asyncio.to_thread(self._parse_pages, path)
        normalized = dict(normalized_pages(pages))
        self._page_text_cache[key] = normalized
        self._page_text_cache.move_to_end(key)
        while len(self._page_text_cache) > _MAX_CACHED_PAGE_TEXTS:
            self._page_text_cache.popitem(last=False)
        return normalized

    @staticmethod
    def _artifact_filename(source: Mapping[str, Any]) -> str | None:
        raw_path = source.get("original_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        return Path(raw_path).name

    @staticmethod
    def _primary_paper_id(source: Mapping[str, Any]) -> str | None:
        metadata = source.get("metadata")
        literature = metadata.get("literature") if isinstance(metadata, Mapping) else None
        if not isinstance(literature, Mapping):
            return None
        paper_id = literature.get("primary_paper_id")
        return str(paper_id) if isinstance(paper_id, str) and paper_id else None

    @staticmethod
    def _evidence_error(
        error: EvidenceResolutionError,
        *,
        source_id: str,
        chunk_id: str,
    ) -> LiteratureServiceError:
        return LiteratureServiceError(
            LiteratureServiceErrorCode.EVIDENCE_UNRESOLVED,
            "该检索块无法核验回原文页码与精确原文",
            details={
                "source_id": source_id,
                "chunk_id": chunk_id,
                "code": error.code.value,
                **deepcopy(error.details),
            },
        )

    def _now_datetime(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_factory must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return self._now_datetime().isoformat()


__all__ = [
    "ArtifactStore",
    "IdentityKind",
    "ImportDisposition",
    "LiteratureEvidenceResult",
    "LiteratureFullTextResult",
    "LiteratureImportBatch",
    "LiteratureImportItem",
    "LiteratureIndexResult",
    "LiteratureSearchExecution",
    "LiteratureService",
    "LiteratureServiceError",
    "LiteratureServiceErrorCode",
    "ProjectSourceStore",
]
