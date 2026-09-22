"""Serializable contracts for the literature-research PoC.

These models deliberately separate global paper identity (``paper_id``) from a
project-local source identity (``source_id``).  They contain no persistence,
network, parser, RAG, or LLM orchestration logic; those belong to later phases.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

NORMALIZATION_VERSION = "literature-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class _FrozenList(list):
    """JSON-serializable list that cannot invalidate a validated snapshot.

    ``list.__init__`` is blocked as well, because re-running it on an existing
    instance would mutate a value the snapshot hash was computed from.  Use
    :meth:`_build` to construct an instance.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract collections must be built with _build()")

    @classmethod
    def _build(cls, iterable: Iterable[Any] = ()) -> _FrozenList:
        instance = list.__new__(cls)
        list.__init__(instance, iterable)
        return instance

    @staticmethod
    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract collections are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    __iadd__ = _blocked
    __imul__ = _blocked
    append = _blocked
    clear = _blocked
    extend = _blocked
    insert = _blocked
    pop = _blocked
    remove = _blocked
    reverse = _blocked
    sort = _blocked

    def __hash__(self) -> int:
        return hash(tuple(self))

    def __copy__(self) -> _FrozenList:
        return type(self)._build(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenList:
        copied = type(self)._build(deepcopy(item, memo) for item in self)
        memo[id(self)] = copied
        return copied

    def __reduce__(self) -> tuple[Any, tuple[list[Any]]]:
        return (type(self)._build, (list(self),))


class _FrozenDict(dict):
    """JSON-serializable dict that cannot invalidate a validated snapshot."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract collections must be built with _build()")

    @classmethod
    def _build(cls, items: Iterable[tuple[Any, Any]] = ()) -> _FrozenDict:
        instance = dict.__new__(cls)
        dict.__init__(instance, items)
        return instance

    @staticmethod
    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("contract collections are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    __ior__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked

    def __hash__(self) -> int:
        return hash(frozenset(self.items()))

    def __copy__(self) -> _FrozenDict:
        return type(self)._build(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenDict:
        copied = type(self)._build(
            (deepcopy(key, memo), deepcopy(value, memo)) for key, value in self.items()
        )
        memo[id(self)] = copied
        return copied

    def __reduce__(self) -> tuple[Any, tuple[dict[Any, Any]]]:
        return (type(self)._build, (dict(self),))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict._build((key, _freeze_json(item)) for key, item in value.items())
    if isinstance(value, list):
        return _FrozenList._build(_freeze_json(item) for item in value)
    return value


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def _validate_http_url(value: str, field_name: str) -> str:
    normalized = value.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return normalized


def _validate_sha256(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex digest")
    return normalized


def _normalize_doi(value: str) -> str:
    normalized = value.strip()
    normalized = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", normalized, flags=re.I)
    normalized = re.sub(r"^https?://(?:www\.)?doi\.org/", "", normalized, flags=re.I)
    normalized = re.sub(r"^(?:doi:|doi\s+|info:doi/)", "", normalized, flags=re.I)
    normalized = normalized.split("?", 1)[0].split("#", 1)[0]
    return normalized.strip().lower()


def _split_arxiv_identifier(value: str) -> tuple[str, int | None]:
    normalized = value.strip()
    normalized = re.sub(r"^https?://arxiv\.org/(?:abs|pdf)/", "", normalized, flags=re.I)
    normalized = re.sub(r"\.pdf$", "", normalized, flags=re.I)
    normalized = re.sub(r"^arxiv:\s*", "", normalized, flags=re.I)
    version_match = re.search(r"v(\d+)$", normalized, flags=re.I)
    if version_match is None:
        return normalized, None
    return normalized[: version_match.start()], int(version_match.group(1))


def make_paper_id(provider: str, provider_record_id: str) -> str:
    """Build a deterministic global paper ID from provider identity."""

    provider_key = provider.strip().casefold()
    record_key = provider_record_id.strip()
    if not provider_key or not record_key:
        raise ValueError("provider and provider_record_id are required")
    digest = _text_hash(f"{provider_key}:{record_key}")[:24]
    return f"paper_{digest}"


class ContractModel(BaseModel):
    """Strict JSON-safe base for cross-layer literature contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy the model and keep collection fields frozen.

        ``BaseModel.model_copy`` intentionally skips validation, so an
        ``update`` value would otherwise bypass the field validators and hand
        back a mutable ``list``/``dict`` that can invalidate the snapshot hash
        the instance was validated with.  Values themselves are still not
        re-validated here (callers that need that use ``model_validate``); only
        the collection immutability is restored.
        """

        copied = super().model_copy(update=update, deep=deep)
        if update:
            for name in type(self).model_fields:
                value = getattr(copied, name)
                if isinstance(value, (list, dict)) and not isinstance(
                    value, (_FrozenList, _FrozenDict)
                ):
                    object.__setattr__(copied, name, _freeze_json(value))
        return copied


class SearchResultMode(StrEnum):
    LIVE = "live"
    CACHE = "cache"
    FIXTURE = "fixture"


class SearchSortBy(StrEnum):
    RELEVANCE = "relevance"
    YEAR = "year"


class SearchSortOrder(StrEnum):
    DESCENDING = "descending"
    ASCENDING = "ascending"


class SearchGenerationMethod(StrEnum):
    USER = "user"
    TEMPLATE = "template"
    LLM = "llm"


class AccessKind(StrEnum):
    PDF = "pdf"
    HTML = "html"
    LANDING_PAGE = "landing_page"
    REPOSITORY = "repository"


class AccessStatus(StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"


class FullTextStatus(StrEnum):
    METADATA_ONLY = "metadata_only"
    ACQUIRING = "acquiring"
    FULLTEXT_READY = "fulltext_ready"
    ACCESS_UNAVAILABLE = "access_unavailable"
    ACQUIRE_FAILED = "acquire_failed"
    PARSING = "parsing"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    INDEXING = "indexing"
    INDEXED = "indexed"
    INDEX_FAILED = "index_failed"


class EvidenceStatus(StrEnum):
    SUPPORTED = "supported"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"


class SearchFilters(ContractModel):
    year_from: int | None = Field(default=None, ge=1000, le=3000)
    year_to: int | None = Field(default=None, ge=1000, le=3000)
    categories: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Provider categories matched with OR semantics.",
    )

    @field_validator("categories")
    @classmethod
    def normalize_categories(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            key = item.casefold()
            if item and key not in seen:
                normalized.append(item)
                seen.add(key)
        return _FrozenList._build(normalized)

    @model_validator(mode="after")
    def validate_year_range(self) -> Self:
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from cannot be later than year_to")
        return self


class SearchQuery(ContractModel):
    query: str = Field(
        min_length=1,
        max_length=2000,
        description="Confirmed provider-native query expression; providers must not reinterpret it.",
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    sort_by: SearchSortBy = SearchSortBy.RELEVANCE
    sort_order: SearchSortOrder = SearchSortOrder.DESCENDING
    filters: SearchFilters = Field(default_factory=SearchFilters)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = _collapse_whitespace(value)
        if not normalized:
            raise ValueError("query cannot be blank")
        return normalized


class SearchPlanDraft(ContractModel):
    """User-visible search intent before server-owned execution fields exist."""

    research_question: str = Field(min_length=1, max_length=4000)
    suggested_query: str = Field(min_length=1, max_length=2000)
    generation_method: SearchGenerationMethod
    generation_model: str | None = Field(default=None, max_length=200)
    generation_config: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("research_question", "suggested_query")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = _collapse_whitespace(value)
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("generation_model")
    @classmethod
    def normalize_generation_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapse_whitespace(value) or None

    @field_validator("generation_config")
    @classmethod
    def freeze_generation_config(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _freeze_json(value)

    @model_validator(mode="after")
    def validate_generation(self) -> Self:
        if self.generation_method is SearchGenerationMethod.LLM and not self.generation_model:
            raise ValueError("generation_model is required for an LLM-generated query")
        if self.generation_method is not SearchGenerationMethod.LLM and self.generation_model:
            raise ValueError("generation_model is only valid for an LLM-generated query")
        return self


class SearchPlan(ContractModel):
    research_question: str = Field(min_length=1, max_length=4000)
    suggested_query: str = Field(min_length=1, max_length=2000)
    executed_query: SearchQuery
    provider: str = Field(min_length=1, max_length=64)
    generation_method: SearchGenerationMethod
    generation_model: str | None = Field(default=None, max_length=200)
    generation_config: dict[str, JsonValue] = Field(default_factory=dict)
    confirmed_at: datetime
    executed_at: datetime
    result_snapshot_id: str = Field(pattern=r"^search_[0-9a-f]{24}$")

    @field_validator("research_question", "suggested_query")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = _collapse_whitespace(value)
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("provider cannot be blank")
        return normalized

    @field_validator("generation_model")
    @classmethod
    def normalize_generation_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapse_whitespace(value) or None

    @field_validator("generation_config")
    @classmethod
    def freeze_generation_config(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _freeze_json(value)

    @field_validator("confirmed_at")
    @classmethod
    def validate_confirmed_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, "confirmed_at")

    @field_validator("executed_at")
    @classmethod
    def validate_executed_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, "executed_at")

    @model_validator(mode="after")
    def validate_execution(self) -> Self:
        if self.generation_method is SearchGenerationMethod.LLM and not self.generation_model:
            raise ValueError("generation_model is required for an LLM-generated query")
        if self.generation_method is not SearchGenerationMethod.LLM and self.generation_model:
            raise ValueError("generation_model is only valid for an LLM-generated query")
        if self.executed_at < self.confirmed_at:
            raise ValueError("executed_at cannot be earlier than confirmed_at")
        return self


class AccessLocation(ContractModel):
    kind: AccessKind
    url: str = Field(min_length=1, max_length=4000)
    access_status: AccessStatus = AccessStatus.UNKNOWN
    mime_type: str | None = Field(default=None, max_length=200)
    license: str | None = Field(default=None, max_length=500)
    is_primary: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_http_url(value, "url")


class ExternalIdentifiers(ContractModel):
    doi: str | None = Field(default=None, max_length=500)
    arxiv: str | None = Field(default=None, max_length=128)
    arxiv_version: int | None = Field(default=None, ge=1)
    other: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_known_identifiers(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_doi = data.get("doi")
        if raw_doi:
            data["doi"] = _normalize_doi(str(raw_doi))
        raw_arxiv = data.get("arxiv")
        if raw_arxiv:
            arxiv_id, embedded_version = _split_arxiv_identifier(str(raw_arxiv))
            data["arxiv"] = arxiv_id
            explicit_version = data.get("arxiv_version")
            if explicit_version is not None:
                explicit_version = int(str(explicit_version).lower().removeprefix("v"))
                if embedded_version is not None and embedded_version != explicit_version:
                    raise ValueError("arxiv identifier version conflicts with arxiv_version")
                data["arxiv_version"] = explicit_version
            elif embedded_version is not None:
                data["arxiv_version"] = embedded_version
        return data

    @field_validator("doi", "arxiv")
    @classmethod
    def reject_blank_known_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("other")
    @classmethod
    def normalize_other_identifiers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            key = raw_key.strip().casefold()
            item = raw_value.strip()
            if not key or not item:
                raise ValueError("other identifier names and values cannot be blank")
            normalized[key] = item
        return _FrozenDict._build(normalized)

    @model_validator(mode="after")
    def validate_arxiv_version(self) -> Self:
        if self.arxiv_version is not None and self.arxiv is None:
            raise ValueError("arxiv_version requires arxiv")
        return self

    def lookup_values(self) -> list[str]:
        values: list[str] = []
        if self.doi:
            values.append(self.doi)
        if self.arxiv:
            values.extend(
                [
                    self.arxiv,
                    f"{self.arxiv}v{self.arxiv_version}"
                    if self.arxiv_version is not None
                    else self.arxiv,
                ]
            )
        values.extend(self.other.values())
        return list(dict.fromkeys(values))


class PaperRecord(ContractModel):
    paper_id: str = ""
    provider: str = Field(min_length=1, max_length=64)
    provider_record_id: str = Field(min_length=1, max_length=500)
    external_ids: ExternalIdentifiers = Field(default_factory=ExternalIdentifiers)
    title: str = Field(min_length=1, max_length=2000)
    authors: list[str] = Field(min_length=1, max_length=500)
    year: int | None = Field(default=None, ge=1000, le=3000)
    venue: str | None = Field(default=None, max_length=1000)
    abstract: str = Field(default="", max_length=100_000)
    categories: list[str] = Field(default_factory=list, max_length=100)
    record_url: str = Field(min_length=1, max_length=4000)
    access_locations: list[AccessLocation] = Field(default_factory=list, max_length=50)
    source_query: str = Field(min_length=1, max_length=10_000)
    retrieved_at: datetime
    normalization_version: str = Field(default=NORMALIZATION_VERSION, min_length=1, max_length=64)
    metadata_snapshot_hash: str = ""

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("provider cannot be blank")
        return normalized

    @field_validator("access_locations")
    @classmethod
    def freeze_access_locations(cls, values: list[AccessLocation]) -> list[AccessLocation]:
        return _FrozenList._build(values)

    @field_validator("provider_record_id", "title", "source_query")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = _collapse_whitespace(value)
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized

    @field_validator("authors", "categories")
    @classmethod
    def normalize_string_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = _collapse_whitespace(value)
            key = item.casefold()
            if item and key not in seen:
                normalized.append(item)
                seen.add(key)
        return _FrozenList._build(normalized)

    @field_validator("venue")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _collapse_whitespace(value) or None

    @field_validator("abstract")
    @classmethod
    def normalize_abstract(cls, value: str) -> str:
        return _collapse_whitespace(value)

    @field_validator("record_url")
    @classmethod
    def validate_record_url(cls, value: str) -> str:
        return _validate_http_url(value, "record_url")

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, "retrieved_at")

    @model_validator(mode="after")
    def finalize_identity_and_hash(self) -> Self:
        if not self.authors:
            raise ValueError("authors must contain at least one non-blank author")
        if sum(location.is_primary for location in self.access_locations) > 1:
            raise ValueError("access_locations can contain at most one primary location")

        expected_paper_id = make_paper_id(self.provider, self.provider_record_id)
        if self.paper_id:
            if not _SAFE_ID_RE.fullmatch(self.paper_id):
                raise ValueError("paper_id contains unsupported characters")
            if self.paper_id != expected_paper_id:
                raise ValueError("paper_id does not match provider identity")
        else:
            object.__setattr__(self, "paper_id", expected_paper_id)

        snapshot_payload = {
            "provider": self.provider,
            "provider_record_id": self.provider_record_id,
            "external_ids": self.external_ids.model_dump(mode="json"),
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "abstract": self.abstract,
            "categories": self.categories,
            "record_url": self.record_url,
            "access_locations": [
                location.model_dump(mode="json") for location in self.access_locations
            ],
            "normalization_version": self.normalization_version,
        }
        expected_hash = _canonical_hash(snapshot_payload)
        if self.metadata_snapshot_hash:
            supplied_hash = _validate_sha256(self.metadata_snapshot_hash, "metadata_snapshot_hash")
            if supplied_hash != expected_hash:
                raise ValueError("metadata_snapshot_hash does not match normalized metadata")
            object.__setattr__(self, "metadata_snapshot_hash", supplied_hash)
        else:
            object.__setattr__(self, "metadata_snapshot_hash", expected_hash)
        return self


class ProviderCapabilities(ContractModel):
    provider: str = Field(min_length=1, max_length=64)
    result_mode: SearchResultMode
    supports_search: bool = True
    supports_record_lookup: bool = True
    supports_access_resolution: bool = True
    supports_fulltext_download: bool = False
    supports_pagination: bool = True
    max_page_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("provider cannot be blank")
        return normalized


class SearchPage(ContractModel):
    provider: str = Field(min_length=1, max_length=64)
    result_mode: SearchResultMode
    query: SearchQuery
    records: list[PaperRecord] = Field(default_factory=list)
    total_results: int | None = Field(default=None, ge=0)
    has_more: bool = False
    retrieved_at: datetime
    result_snapshot_id: str = ""
    provenance_label: str | None = Field(default=None, max_length=500)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("provider cannot be blank")
        return normalized

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, "retrieved_at")

    @field_validator("records")
    @classmethod
    def freeze_records(cls, values: list[PaperRecord]) -> list[PaperRecord]:
        return _FrozenList._build(values)

    @model_validator(mode="after")
    def finalize_snapshot(self) -> Self:
        if self.total_results is not None and self.total_results < len(self.records):
            raise ValueError("total_results cannot be smaller than the returned record count")
        if self.total_results is not None:
            returned_through = (self.query.page - 1) * self.query.page_size + len(self.records)
            expected_has_more = returned_through < self.total_results
            if self.has_more is not expected_has_more:
                raise ValueError("has_more contradicts query pagination and total_results")
        if self.result_mode in {
            SearchResultMode.CACHE,
            SearchResultMode.FIXTURE,
        } and (not self.provenance_label or not self.provenance_label.strip()):
            raise ValueError("cache and fixture results require a provenance_label")

        snapshot_payload = {
            "provider": self.provider,
            "result_mode": self.result_mode.value,
            "query": self.query.model_dump(mode="json"),
            "papers": [
                {
                    "paper_id": record.paper_id,
                    "metadata_snapshot_hash": record.metadata_snapshot_hash,
                }
                for record in self.records
            ],
            "total_results": self.total_results,
            "has_more": self.has_more,
            "provenance_label": self.provenance_label,
        }
        expected_snapshot_id = f"search_{_canonical_hash(snapshot_payload)[:24]}"
        if self.result_snapshot_id:
            if self.result_snapshot_id != expected_snapshot_id:
                raise ValueError("result_snapshot_id does not match the returned search page")
        else:
            object.__setattr__(self, "result_snapshot_id", expected_snapshot_id)
        return self


_FILE_PRESENT_STATES = {
    FullTextStatus.FULLTEXT_READY,
    FullTextStatus.PARSING,
    FullTextStatus.PARSED,
    FullTextStatus.PARSE_FAILED,
    FullTextStatus.INDEXING,
    FullTextStatus.INDEXED,
    FullTextStatus.INDEX_FAILED,
}
_FAILURE_STATES = {
    FullTextStatus.ACCESS_UNAVAILABLE,
    FullTextStatus.ACQUIRE_FAILED,
    FullTextStatus.PARSE_FAILED,
    FullTextStatus.INDEX_FAILED,
}


class FullTextArtifact(ContractModel):
    """Full-text state for one project source; ``source_id`` is project-local."""

    source_id: str = Field(min_length=1, max_length=64)
    status: FullTextStatus
    artifact_id: str | None = Field(default=None, max_length=128)
    local_path: str | None = Field(default=None, max_length=4000)
    source_url: str | None = Field(default=None, max_length=4000)
    access_status: AccessStatus = AccessStatus.UNKNOWN
    acquired_at: datetime | None = None
    file_size_bytes: int | None = Field(default=None, gt=0)
    mime_type: str | None = Field(default=None, max_length=200)
    sha256: str | None = None
    artifact_version: int = Field(default=1, ge=1)
    failure_reason: str | None = Field(default=None, max_length=4000)

    @field_validator("source_id")
    @classmethod
    def normalize_source_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_id cannot be blank")
        return normalized

    @field_validator("local_path", "mime_type", "failure_reason")
    @classmethod
    def normalize_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_http_url(value, "source_url")

    @field_validator("acquired_at")
    @classmethod
    def validate_acquired_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_aware_datetime(value, "acquired_at")

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_sha256(value, "sha256")

    @model_validator(mode="after")
    def validate_state_payload(self) -> Self:
        if self.status in _FILE_PRESENT_STATES:
            required = {
                "local_path": self.local_path,
                "acquired_at": self.acquired_at,
                "file_size_bytes": self.file_size_bytes,
                "mime_type": self.mime_type,
                "sha256": self.sha256,
            }
            missing = [name for name, value in required.items() if value in {None, ""}]
            if missing:
                raise ValueError(
                    f"status {self.status.value} requires file fields: {', '.join(missing)}"
                )
            expected_artifact_id = f"artifact_{_text_hash(f'{self.source_id}:{self.sha256}')[:24]}"
            if self.artifact_id and self.artifact_id != expected_artifact_id:
                raise ValueError("artifact_id does not match source_id and sha256")
            object.__setattr__(self, "artifact_id", expected_artifact_id)
        elif self.artifact_id is not None:
            raise ValueError("artifact_id is only valid after a full-text file exists")

        if self.status not in _FILE_PRESENT_STATES:
            unexpected_file_fields = {
                "local_path": self.local_path,
                "acquired_at": self.acquired_at,
                "file_size_bytes": self.file_size_bytes,
                "mime_type": self.mime_type,
                "sha256": self.sha256,
            }
            present = [name for name, value in unexpected_file_fields.items() if value is not None]
            if present:
                raise ValueError(
                    f"status {self.status.value} cannot contain final file fields: "
                    + ", ".join(present)
                )

        if self.status in _FAILURE_STATES and (
            not self.failure_reason or not self.failure_reason.strip()
        ):
            raise ValueError(f"status {self.status.value} requires failure_reason")
        if self.status not in _FAILURE_STATES and self.failure_reason is not None:
            raise ValueError(f"status {self.status.value} cannot contain failure_reason")
        return self


class EvidenceSpan(ContractModel):
    """One verifiable quote in normalized, 1-based PDF page text."""

    evidence_id: str = ""
    source_id: str = Field(min_length=1, max_length=64)
    artifact_sha256: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    coordinate_space: Literal["normalized_page_text_v1"] = "normalized_page_text_v1"
    chunk_id: str = Field(min_length=1, max_length=128)
    exact_quote: str = Field(min_length=1, max_length=100_000)
    quote_sha256: str = ""
    context_before: str = Field(default="", max_length=100_000)
    context_after: str = Field(default="", max_length=100_000)
    parser_version: str = Field(min_length=1, max_length=128)
    chunker_version: str = Field(min_length=1, max_length=128)
    embedding_model: str = Field(min_length=1, max_length=500)
    embedding_version: str = Field(min_length=1, max_length=128)
    index_version: str = Field(min_length=1, max_length=128)

    @field_validator("artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "artifact_sha256")

    @field_validator("source_id", "chunk_id")
    @classmethod
    def normalize_evidence_id_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence identifiers cannot be blank")
        return normalized

    @model_validator(mode="after")
    def finalize_coordinates_and_hash(self) -> Self:
        if self.page_start != self.page_end:
            raise ValueError("first-version evidence spans cannot cross PDF pages")
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if not self.exact_quote.strip():
            raise ValueError("exact_quote cannot be blank")
        if self.char_end - self.char_start != len(self.exact_quote):
            raise ValueError("page character coordinates must exactly bound exact_quote")

        expected_quote_hash = _text_hash(self.exact_quote)
        if self.quote_sha256:
            supplied_hash = _validate_sha256(self.quote_sha256, "quote_sha256")
            if supplied_hash != expected_quote_hash:
                raise ValueError("quote_sha256 does not match exact_quote")
            object.__setattr__(self, "quote_sha256", supplied_hash)
        else:
            object.__setattr__(self, "quote_sha256", expected_quote_hash)

        expected_evidence_id = (
            "evidence_"
            + _text_hash(
                ":".join(
                    [
                        self.source_id,
                        self.artifact_sha256,
                        str(self.page_start),
                        str(self.char_start),
                        str(self.char_end),
                        self.chunk_id,
                        self.quote_sha256,
                    ]
                )
            )[:24]
        )
        if self.evidence_id:
            if self.evidence_id != expected_evidence_id:
                raise ValueError("evidence_id does not match evidence coordinates")
        else:
            object.__setattr__(self, "evidence_id", expected_evidence_id)
        return self


class AnswerClaim(ContractModel):
    claim_id: str = ""
    text: str = Field(min_length=1, max_length=20_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_status: EvidenceStatus
    model_provider: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=500)
    model_config_hash: str
    generated_at: datetime

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = _collapse_whitespace(value)
        if not normalized:
            raise ValueError("text cannot be blank")
        return normalized

    @field_validator("model_config_hash")
    @classmethod
    def validate_model_config_hash(cls, value: str) -> str:
        return _validate_sha256(value, "model_config_hash")

    @field_validator("evidence_ids")
    @classmethod
    def freeze_evidence_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("evidence_ids cannot contain blank values")
        return _FrozenList._build(normalized)

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value, "generated_at")

    @model_validator(mode="after")
    def finalize_claim(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids cannot contain duplicates")
        if self.evidence_status is not EvidenceStatus.INSUFFICIENT and not self.evidence_ids:
            raise ValueError("supported or conflicting claims require evidence_ids")

        expected_claim_id = (
            "claim_"
            + _canonical_hash(
                {
                    "text": self.text,
                    "evidence_ids": self.evidence_ids,
                    "evidence_status": self.evidence_status.value,
                    "model_provider": self.model_provider,
                    "model_name": self.model_name,
                    "model_config_hash": self.model_config_hash,
                }
            )[:24]
        )
        if self.claim_id:
            if self.claim_id != expected_claim_id:
                raise ValueError("claim_id does not match claim content")
        else:
            object.__setattr__(self, "claim_id", expected_claim_id)
        return self
