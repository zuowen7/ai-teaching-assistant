"""Provider contract and domain-level failure semantics for literature discovery."""

from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from src.literature.models import (
    AccessLocation,
    PaperRecord,
    ProviderCapabilities,
    SearchPage,
    SearchQuery,
)


class ProviderOperation(StrEnum):
    SEARCH = "search"
    GET_RECORD = "get_record"
    RESOLVE_ACCESS = "resolve_access"


class ProviderErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


_DEFAULT_STATUS_CODE = {
    ProviderErrorCode.INVALID_REQUEST: 400,
    ProviderErrorCode.NOT_FOUND: 404,
    ProviderErrorCode.RATE_LIMITED: 429,
    ProviderErrorCode.UNAVAILABLE: 503,
    ProviderErrorCode.INVALID_RESPONSE: 502,
}
_RETRYABLE_CODES = {
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.UNAVAILABLE,
}


class LiteratureProviderError(RuntimeError):
    """A typed provider failure that cannot be confused with a valid empty result."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        provider: str,
        operation: ProviderOperation,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider.strip().casefold()
        self.operation = operation
        self.status_code = status_code if status_code is not None else _DEFAULT_STATUS_CODE[code]
        if not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be between 100 and 599")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")
        self.retry_after_seconds = retry_after_seconds
        self.details = deepcopy(details or {})

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE_CODES

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "provider": self.provider,
            "operation": self.operation.value,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "details": deepcopy(self.details),
        }

    def clone(self) -> LiteratureProviderError:
        """Return a fresh exception for deterministic fixture fault injection."""

        return LiteratureProviderError(
            self.code,
            str(self),
            provider=self.provider,
            operation=self.operation,
            status_code=self.status_code,
            retry_after_seconds=self.retry_after_seconds,
            details=self.details,
        )


@runtime_checkable
class LiteratureProvider(Protocol):
    """Replaceable metadata discovery contract.

    Providers discover and normalize records.  They may resolve candidate
    access locations, but they do not download, parse, index, or answer from
    full text.
    """

    @property
    def name(self) -> str: ...

    async def search(self, query: SearchQuery) -> SearchPage: ...

    async def get_record(self, external_id: str) -> PaperRecord: ...

    async def resolve_access(self, record: PaperRecord) -> list[AccessLocation]: ...

    def capabilities(self) -> ProviderCapabilities: ...


__all__ = [
    "LiteratureProvider",
    "LiteratureProviderError",
    "ProviderErrorCode",
    "ProviderOperation",
]
