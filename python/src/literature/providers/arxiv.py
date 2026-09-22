"""Structured arXiv metadata provider for the literature-research PoC."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse
from weakref import WeakKeyDictionary
from xml.etree import ElementTree as ET

import httpx
from pydantic import ValidationError

from src.literature.models import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    PaperRecord,
    ProviderCapabilities,
    SearchPage,
    SearchQuery,
    SearchResultMode,
    SearchSortBy,
)
from src.literature.providers.base import (
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)

ARXIV_API_ENDPOINT = "https://export.arxiv.org/api/query"
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_ARXIV_ID_RE = re.compile(
    r"^(?P<base>(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z0-9.-]*/\d{7}))"
    r"(?:v(?P<version>[1-9]\d*))?$",
    flags=re.IGNORECASE,
)
_ARXIV_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")
_ARXIV_XML_MEDIA_TYPES = {
    "application/atom+xml",
    "application/xml",
    "text/xml",
}
_ATOM_NS = "http://www.w3.org/2005/Atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_NS = {
    "atom": _ATOM_NS,
    "opensearch": _OPENSEARCH_NS,
    "arxiv": _ARXIV_NS,
}


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _normalize_arxiv_identifier(value: str) -> tuple[str, int | None]:
    raw = value.strip()
    if not raw:
        raise ValueError("arXiv identifier cannot be blank")

    raw = re.sub(r"^arxiv:\s*", "", raw, flags=re.IGNORECASE)
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "arxiv.org",
            "www.arxiv.org",
        }:
            raise ValueError("external_id must be an arXiv identifier or arXiv URL")
        raw = unquote(parsed.path)
    raw = re.sub(r"^/(?:abs|pdf)/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(?:abs|pdf)/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\.pdf$", "", raw, flags=re.IGNORECASE)

    match = _ARXIV_ID_RE.fullmatch(raw)
    if match is None:
        raise ValueError("external_id is not a supported arXiv identifier")
    base = match.group("base").casefold()
    version_text = match.group("version")
    return base, int(version_text) if version_text is not None else None


def _full_arxiv_identifier(base: str, version: int | None) -> str:
    return f"{base}v{version}" if version is not None else base


def _required_text(element: ET.Element, path: str, field_name: str) -> str:
    child = element.find(path, _NS)
    value = _collapse_whitespace(child.text or "") if child is not None else ""
    if not value:
        raise ValueError(f"arXiv entry is missing {field_name}")
    return value


def _optional_text(element: ET.Element, path: str) -> str | None:
    child = element.find(path, _NS)
    if child is None:
        return None
    value = _collapse_whitespace(child.text or "")
    return value or None


def _parse_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"arXiv {field_name} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"arXiv {field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class ArxivRequestGate:
    """Serialize requests and space their start times for one event loop."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._lock = asyncio.Lock()
        self._last_request_started_at: float | None = None

    @asynccontextmanager
    async def request_slot(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._last_request_started_at is not None:
                elapsed = self._clock() - self._last_request_started_at
                remaining = self._min_interval_seconds - elapsed
                if remaining > 0:
                    await self._sleeper(remaining)
            self._last_request_started_at = self._clock()
            yield


_DEFAULT_REQUEST_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, ArxivRequestGate] = (
    WeakKeyDictionary()
)


def _default_request_gate() -> ArxivRequestGate:
    loop = asyncio.get_running_loop()
    gate = _DEFAULT_REQUEST_GATES.get(loop)
    if gate is None:
        gate = ArxivRequestGate()
        _DEFAULT_REQUEST_GATES[loop] = gate
    return gate


class ArxivProvider:
    """Live arXiv Atom provider with serialized, rate-limited requests.

    Default instances on the same event loop share a gate that permits one
    in-flight request and spaces request starts by three seconds.  Multi-process
    coordination is outside this PoC.  Tests may inject a private zero-delay
    gate without weakening the production default.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        endpoint: str = ARXIV_API_ENDPOINT,
        timeout_seconds: float = 30.0,
        request_gate: ArxivRequestGate | None = None,
        user_agent: str = "ScholarAssistant-PoC/0.1",
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
            raise ValueError("arXiv endpoint must be an absolute HTTPS URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        normalized_user_agent = user_agent.strip()
        if not normalized_user_agent:
            raise ValueError("user_agent cannot be blank")

        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._request_gate = request_gate
        self._user_agent = normalized_user_agent
        self._now_factory = now_factory or (lambda: datetime.now(UTC))
        self._client = client
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return "arxiv"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            result_mode=SearchResultMode.LIVE,
            supports_search=True,
            supports_record_lookup=True,
            supports_access_resolution=True,
            supports_fulltext_download=False,
            supports_pagination=True,
            max_page_size=100,
        )

    async def __aenter__(self) -> ArxivProvider:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: SearchQuery) -> SearchPage:
        validated_query = SearchQuery.model_validate(query.model_dump(mode="json"))
        expression = self._build_search_expression(validated_query)
        start = (validated_query.page - 1) * validated_query.page_size
        params = {
            "search_query": expression,
            "start": str(start),
            "max_results": str(validated_query.page_size),
            "sortBy": (
                "relevance"
                if validated_query.sort_by is SearchSortBy.RELEVANCE
                else "submittedDate"
            ),
            "sortOrder": validated_query.sort_order.value,
        }
        response = await self._request(params, ProviderOperation.SEARCH)
        retrieved_at = self._current_time()
        total_results, records = self._parse_feed(
            response.content,
            source_query=expression,
            retrieved_at=retrieved_at,
            operation=ProviderOperation.SEARCH,
            expected_start=start,
            expected_max_results=validated_query.page_size,
        )
        return SearchPage(
            provider=self.name,
            result_mode=SearchResultMode.LIVE,
            query=validated_query,
            records=records,
            total_results=total_results,
            has_more=start + len(records) < total_results,
            retrieved_at=retrieved_at,
        )

    async def get_record(self, external_id: str) -> PaperRecord:
        try:
            base, requested_version = _normalize_arxiv_identifier(external_id)
        except ValueError as exc:
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                str(exc),
                provider=self.name,
                operation=ProviderOperation.GET_RECORD,
            ) from exc
        requested_id = _full_arxiv_identifier(base, requested_version)
        response = await self._request(
            {"id_list": requested_id, "max_results": "1"},
            ProviderOperation.GET_RECORD,
        )
        retrieved_at = self._current_time()
        total_results, records = self._parse_feed(
            response.content,
            source_query=f"id_list:{requested_id}",
            retrieved_at=retrieved_at,
            operation=ProviderOperation.GET_RECORD,
            expected_start=0,
            expected_max_results=1,
        )
        if total_results == 0 or not records:
            raise LiteratureProviderError(
                ProviderErrorCode.NOT_FOUND,
                f"arXiv record not found: {external_id}",
                provider=self.name,
                operation=ProviderOperation.GET_RECORD,
            )
        if total_results != 1 or len(records) != 1 or records[0].provider_record_id != base:
            raise self._invalid_response(
                ProviderOperation.GET_RECORD,
                "arXiv returned an unexpected record for id_list lookup",
            )
        returned_version = records[0].external_ids.arxiv_version
        if requested_version is not None and returned_version != requested_version:
            raise self._invalid_response(
                ProviderOperation.GET_RECORD,
                "arXiv returned a different article version than requested",
            )
        return records[0]

    async def resolve_access(self, record: PaperRecord) -> list[AccessLocation]:
        validated_record = PaperRecord.model_validate(record.model_dump(mode="json"))
        if validated_record.provider != self.name:
            raise LiteratureProviderError(
                ProviderErrorCode.NOT_FOUND,
                f"record does not belong to provider {self.name}: {record.paper_id}",
                provider=self.name,
                operation=ProviderOperation.RESOLVE_ACCESS,
            )
        try:
            base, provider_version = _normalize_arxiv_identifier(
                validated_record.provider_record_id
            )
            if provider_version is not None:
                raise ValueError("arXiv provider_record_id must omit the article version")
            if validated_record.external_ids.arxiv is not None:
                external_base, _ = _normalize_arxiv_identifier(validated_record.external_ids.arxiv)
                if external_base != base:
                    raise ValueError("arXiv external identifier does not match provider_record_id")
        except ValueError as exc:
            raise LiteratureProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                str(exc),
                provider=self.name,
                operation=ProviderOperation.RESOLVE_ACCESS,
            ) from exc
        version = validated_record.external_ids.arxiv_version
        return self._canonical_access_locations(base, version)

    def _build_search_expression(self, query: SearchQuery) -> str:
        clauses = [f"({query.query})"]
        if query.filters.categories:
            category_clauses: list[str] = []
            for category in query.filters.categories:
                if _ARXIV_CATEGORY_RE.fullmatch(category) is None:
                    raise LiteratureProviderError(
                        ProviderErrorCode.INVALID_REQUEST,
                        f"unsupported arXiv category filter: {category!r}",
                        provider=self.name,
                        operation=ProviderOperation.SEARCH,
                    )
                category_clauses.append(f"cat:{category}")
            clauses.append("(" + " OR ".join(category_clauses) + ")")
        if query.filters.year_from is not None or query.filters.year_to is not None:
            year_from = query.filters.year_from or 1000
            year_to = query.filters.year_to or 3000
            clauses.append(f"submittedDate:[{year_from:04d}01010000 TO {year_to:04d}12312359]")
        return " AND ".join(clauses) if len(clauses) > 1 else query.query

    async def _request(
        self,
        params: dict[str, str],
        operation: ProviderOperation,
    ) -> httpx.Response:
        gate = self._request_gate or _default_request_gate()
        async with gate.request_slot():
            try:
                client = self._get_client()
                response = await client.get(
                    self._endpoint,
                    params=params,
                    headers={
                        "Accept": "application/atom+xml, application/xml;q=0.9",
                        "User-Agent": self._user_agent,
                    },
                )
            except httpx.InvalidURL as exc:
                # Client construction/environment failure (for example a proxy
                # variable httpx cannot parse).  It must stay a typed provider
                # failure instead of escaping as an unhandled 500.
                raise LiteratureProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    "arXiv client configuration is unusable",
                    provider=self.name,
                    operation=operation,
                    details={"exception_type": type(exc).__name__},
                ) from exc
            except httpx.TimeoutException as exc:
                raise LiteratureProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    "arXiv request timed out",
                    provider=self.name,
                    operation=operation,
                    details={"exception_type": type(exc).__name__},
                ) from exc
            except httpx.TransportError as exc:
                raise LiteratureProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    "arXiv transport failed",
                    provider=self.name,
                    operation=operation,
                    details={"exception_type": type(exc).__name__},
                ) from exc

        if response.status_code == 200:
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise self._invalid_response(
                    operation,
                    "arXiv response exceeded the configured size limit",
                )
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type not in _ARXIV_XML_MEDIA_TYPES:
                raise self._invalid_response(
                    operation,
                    "arXiv response did not use an expected Atom/XML content type",
                )
            return response

        if response.status_code == 429:
            code = ProviderErrorCode.RATE_LIMITED
        elif response.status_code >= 500:
            code = ProviderErrorCode.UNAVAILABLE
        elif response.status_code == 404 and operation is ProviderOperation.GET_RECORD:
            code = ProviderErrorCode.NOT_FOUND
        elif 400 <= response.status_code < 500 and response.status_code != 404:
            code = ProviderErrorCode.INVALID_REQUEST
        else:
            code = ProviderErrorCode.INVALID_RESPONSE
        raise LiteratureProviderError(
            code,
            f"arXiv API returned HTTP {response.status_code}",
            provider=self.name,
            operation=operation,
            status_code=response.status_code,
            retry_after_seconds=_parse_retry_after(response.headers.get("Retry-After")),
            details={"response_excerpt": response.text[:500]},
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self._timeout_seconds, connect=min(10.0, self._timeout_seconds))
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
        return self._client

    def _current_time(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now_factory must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _parse_feed(
        self,
        content: bytes,
        *,
        source_query: str,
        retrieved_at: datetime,
        operation: ProviderOperation,
        expected_start: int,
        expected_max_results: int,
    ) -> tuple[int, list[PaperRecord]]:
        declaration_scan = content.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
            raise self._invalid_response(operation, "arXiv XML declarations are not allowed")
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise self._invalid_response(operation, "arXiv returned malformed XML") from exc
        if root.tag != f"{{{_ATOM_NS}}}feed":
            raise self._invalid_response(operation, "arXiv response root is not an Atom feed")

        total_text = _optional_text(root, "opensearch:totalResults")
        try:
            total_results = int(total_text) if total_text is not None else -1
        except ValueError as exc:
            raise self._invalid_response(
                operation,
                "arXiv totalResults is not an integer",
            ) from exc
        if total_results < 0:
            raise self._invalid_response(operation, "arXiv response is missing totalResults")

        start_text = _optional_text(root, "opensearch:startIndex")
        try:
            response_start = int(start_text) if start_text is not None else -1
        except ValueError as exc:
            raise self._invalid_response(
                operation,
                "arXiv startIndex is not an integer",
            ) from exc
        if response_start != expected_start:
            raise self._invalid_response(
                operation,
                "arXiv startIndex does not match the requested page",
            )

        entries = root.findall("atom:entry", _NS)
        for entry in entries:
            entry_id = _optional_text(entry, "atom:id") or ""
            title = _optional_text(entry, "atom:title") or ""
            if "/api/errors#" in entry_id or title.casefold() == "error":
                summary = _optional_text(entry, "atom:summary") or "unknown arXiv API error"
                raise LiteratureProviderError(
                    ProviderErrorCode.INVALID_REQUEST,
                    summary,
                    provider=self.name,
                    operation=operation,
                    status_code=400,
                )

        try:
            records = [
                self._parse_entry(
                    entry,
                    source_query=source_query,
                    retrieved_at=retrieved_at,
                )
                for entry in entries
            ]
        except (ValueError, ValidationError) as exc:
            raise self._invalid_response(operation, str(exc)) from exc
        expected_count = min(
            expected_max_results,
            max(total_results - expected_start, 0),
        )
        # arXiv's ``totalResults`` is an estimate, so a short page is accepted:
        # only a page with *more* entries than ``totalResults``, ``start`` and
        # ``max_results`` can account for is a genuine provider contradiction.
        if len(records) > expected_count:
            raise self._invalid_response(
                operation,
                "arXiv returned more entries than totalResults, start, or max_results allow",
            )
        return total_results, records

    def _parse_entry(
        self,
        entry: ET.Element,
        *,
        source_query: str,
        retrieved_at: datetime,
    ) -> PaperRecord:
        entry_id = _required_text(entry, "atom:id", "id")
        base, version = _normalize_arxiv_identifier(entry_id)
        published = _parse_datetime(
            _required_text(entry, "atom:published", "published"),
            "published",
        )
        authors = [
            _collapse_whitespace(name.text or "")
            for name in entry.findall("atom:author/atom:name", _NS)
            if _collapse_whitespace(name.text or "")
        ]
        if not authors:
            raise ValueError("arXiv entry is missing authors")
        categories = [
            term.strip()
            for category in entry.findall("atom:category", _NS)
            if (term := category.attrib.get("term", "")).strip()
        ]
        doi = _optional_text(entry, "arxiv:doi")
        journal_ref = _optional_text(entry, "arxiv:journal_ref")
        return PaperRecord(
            provider=self.name,
            provider_record_id=base,
            external_ids=ExternalIdentifiers(
                doi=doi,
                arxiv=base,
                arxiv_version=version,
            ),
            title=_required_text(entry, "atom:title", "title"),
            authors=authors,
            year=published.year,
            venue=journal_ref or "arXiv",
            abstract=_required_text(entry, "atom:summary", "summary"),
            categories=categories,
            record_url=f"https://arxiv.org/abs/{_full_arxiv_identifier(base, version)}",
            access_locations=self._canonical_access_locations(base, version),
            source_query=source_query,
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _canonical_access_locations(
        base: str,
        version: int | None,
    ) -> list[AccessLocation]:
        full_id = _full_arxiv_identifier(base, version)
        return [
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

    def _invalid_response(
        self,
        operation: ProviderOperation,
        message: str,
        *,
        status_code: int | None = None,
    ) -> LiteratureProviderError:
        return LiteratureProviderError(
            ProviderErrorCode.INVALID_RESPONSE,
            message,
            provider=self.name,
            operation=operation,
            status_code=status_code,
        )


__all__ = [
    "ARXIV_API_ENDPOINT",
    "ARXIV_MIN_REQUEST_INTERVAL_SECONDS",
    "ArxivRequestGate",
    "ArxivProvider",
]
