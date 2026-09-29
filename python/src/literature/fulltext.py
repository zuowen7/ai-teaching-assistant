"""Open-access full-text acquisition for the literature PoC (M5).

The downloader only ever uses locations the provider itself declared as
``open`` PDFs over https.  It enforces a size cap, a timeout, a Content-Type
allow-list and a ``%PDF-`` magic-byte check, and it never writes to disk: the
service decides where an accepted artifact is stored (plan section 5.8, D-023).
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from src.literature.models import AccessKind, AccessLocation, AccessStatus, PaperRecord

MAX_PDF_BYTES = 25 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
USER_AGENT = "ScholarAssistant-PoC/0.1 (+literature-fulltext)"
_ALLOWED_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})
_PDF_MAGIC = b"%PDF-"


class FullTextErrorCode(StrEnum):
    """Explicit reasons an open full text could not be acquired."""

    INVALID_URL = "invalid_url"
    NOT_OPEN_ACCESS = "not_open_access"
    DOWNLOAD_FAILED = "download_failed"
    TOO_LARGE = "too_large"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    NOT_A_PDF = "not_a_pdf"


class FullTextDownloadError(RuntimeError):
    """A download that must not be presented as a valid full text."""

    def __init__(
        self,
        code: FullTextErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class DownloadedArtifact:
    """Bytes accepted as one open-access PDF, with their hash."""

    content: bytes
    sha256: str
    size_bytes: int
    mime_type: str
    source_url: str


@runtime_checkable
class FullTextDownloader(Protocol):
    """Narrow acquisition boundary so tests and demos can stay offline."""

    async def download(self, url: str) -> DownloadedArtifact:
        """Fetch one https URL and return validated PDF bytes."""
        ...

    async def aclose(self) -> None:
        """Release any owned transport resources."""
        ...


def select_open_pdf_location(record: PaperRecord) -> AccessLocation | None:
    """Return the provider-declared open https PDF location, if any.

    Restricted, unknown, non-PDF or non-https locations are ignored rather than
    probed, so the PoC never bypasses access restrictions.
    """

    candidates = [
        location
        for location in record.access_locations
        if location.kind is AccessKind.PDF
        and location.access_status is AccessStatus.OPEN
        and urlparse(location.url).scheme.lower() == "https"
        and bool(urlparse(location.url).netloc)
    ]
    if not candidates:
        return None
    primary = next((location for location in candidates if location.is_primary), None)
    return primary or candidates[0]


class HttpFullTextDownloader:
    """httpx-backed downloader with explicit acceptance rules."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes: int = MAX_PDF_BYTES,
        user_agent: str = USER_AGENT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        normalized_agent = user_agent.strip()
        if not normalized_agent:
            raise ValueError("user_agent cannot be blank")
        self._client = client
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._user_agent = normalized_agent

    async def download(self, url: str) -> DownloadedArtifact:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise FullTextDownloadError(
                FullTextErrorCode.INVALID_URL,
                "only absolute https locations can be acquired",
                details={"url": url},
            )

        client = self._get_client()
        try:
            response = await client.get(
                url,
                headers={"User-Agent": self._user_agent, "Accept": "application/pdf"},
                # Follow redirects per request so an injected client cannot
                # silently change the acquisition semantics.
                follow_redirects=True,
            )
        except httpx.InvalidURL as exc:
            raise FullTextDownloadError(
                FullTextErrorCode.INVALID_URL,
                "the location could not be requested",
                details={"url": url, "exception_type": type(exc).__name__},
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise FullTextDownloadError(
                FullTextErrorCode.DOWNLOAD_FAILED,
                "download failed before a complete response arrived",
                details={"url": url, "exception_type": type(exc).__name__},
            ) from exc

        if response.status_code != 200:
            raise FullTextDownloadError(
                FullTextErrorCode.DOWNLOAD_FAILED,
                f"full text location returned HTTP {response.status_code}",
                details={"url": url, "status_code": response.status_code},
            )

        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > self._max_bytes:
                    raise FullTextDownloadError(
                        FullTextErrorCode.TOO_LARGE,
                        "declared full text size exceeds the configured limit",
                        details={"url": url, "content_length": int(declared_length)},
                    )
            except ValueError:
                pass

        content = response.content
        if len(content) > self._max_bytes:
            raise FullTextDownloadError(
                FullTextErrorCode.TOO_LARGE,
                "full text exceeded the configured size limit",
                details={"url": url, "size_bytes": len(content), "max_bytes": self._max_bytes},
            )

        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _ALLOWED_CONTENT_TYPES:
            raise FullTextDownloadError(
                FullTextErrorCode.UNEXPECTED_CONTENT_TYPE,
                "full text location did not return a PDF content type",
                details={"url": url, "content_type": media_type},
            )
        if not content.startswith(_PDF_MAGIC):
            raise FullTextDownloadError(
                FullTextErrorCode.NOT_A_PDF,
                "full text body is not a PDF",
                details={"url": url, "prefix": content[:8].decode("latin-1", "replace")},
            )

        return DownloadedArtifact(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            mime_type=media_type,
            source_url=url,
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpFullTextDownloader:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self._timeout_seconds, connect=min(10.0, self._timeout_seconds))
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            )
        return self._client


async def maybe_aclose(downloader: object) -> None:
    """Close a downloader if it exposes an awaitable ``aclose``."""

    close = getattr(downloader, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "DOWNLOAD_TIMEOUT_SECONDS",
    "MAX_PDF_BYTES",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "DownloadedArtifact",
    "FullTextDownloadError",
    "FullTextDownloader",
    "FullTextErrorCode",
    "HttpFullTextDownloader",
    "maybe_aclose",
    "select_open_pdf_location",
]
