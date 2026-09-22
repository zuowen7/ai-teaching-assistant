"""HTTP adapter for structured literature discovery and project import."""

from __future__ import annotations

import math
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from routers.project import _read_sources, _require_project, _source_manifest
from src.literature.models import (
    PaperRecord,
    ProviderCapabilities,
    SearchPlanDraft,
    SearchQuery,
)
from src.literature.providers.base import LiteratureProviderError, ProviderErrorCode
from src.literature.service import (
    LiteratureImportBatch,
    LiteratureSearchExecution,
    LiteratureService,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
    ProjectSourceStore,
)
from src.utils.atomic_io import atomic_write_json, locked_path


class LiteratureSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    query: SearchQuery
    plan: SearchPlanDraft

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("provider cannot be blank")
        return normalized


class LiteratureImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1, max_length=1000)
    search_execution_id: str = Field(pattern=r"^search_exec_[0-9a-f]{32}$")
    paper_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("paper_ids")
    @classmethod
    def normalize_paper_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("paper_ids cannot contain blank values")
            if len(item) > 128:
                raise ValueError("paper_id is too long")
            normalized.append(item)
        return normalized


class LiteratureProvidersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderCapabilities]


class ProjectSourceManifestStore(ProjectSourceStore):
    """Narrow adapter over the existing project path and manifest contract."""

    def update_sources(
        self,
        project_path: str,
        update: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    ) -> None:
        project = _require_project(project_path)
        manifest = _source_manifest(project)
        with locked_path(manifest):
            sources = deepcopy(_read_sources(project))
            updated = update(sources)
            if not isinstance(updated, list) or any(not isinstance(item, dict) for item in updated):
                raise HTTPException(500, "项目文献更新结果结构无效")
            if updated != sources:
                atomic_write_json(
                    manifest,
                    {"version": 1, "sources": deepcopy(updated)},
                )


_PROVIDER_MESSAGES = {
    ProviderErrorCode.INVALID_REQUEST: "文献源拒绝了该请求",
    ProviderErrorCode.NOT_FOUND: "文献源中未找到该记录",
    ProviderErrorCode.RATE_LIMITED: "文献源请求过于频繁，请稍后重试",
    ProviderErrorCode.UNAVAILABLE: "文献源当前不可用",
    ProviderErrorCode.INVALID_RESPONSE: "文献源返回了无法验证的响应",
}
_PROVIDER_STATUS = {
    ProviderErrorCode.INVALID_REQUEST: 400,
    ProviderErrorCode.NOT_FOUND: 404,
    ProviderErrorCode.RATE_LIMITED: 429,
    ProviderErrorCode.UNAVAILABLE: 503,
    ProviderErrorCode.INVALID_RESPONSE: 502,
}
_SERVICE_STATUS = {
    LiteratureServiceErrorCode.UNKNOWN_PROVIDER: 404,
    LiteratureServiceErrorCode.SEARCH_EXECUTION_NOT_FOUND: 404,
    LiteratureServiceErrorCode.RECORD_NOT_IN_SNAPSHOT: 400,
    LiteratureServiceErrorCode.IDENTITY_CONFLICT: 409,
    LiteratureServiceErrorCode.PROJECT_DATA_INVALID: 409,
    LiteratureServiceErrorCode.SOURCE_ID_EXHAUSTED: 500,
    LiteratureServiceErrorCode.SEARCH_EXECUTION_ID_EXHAUSTED: 500,
}


def _provider_http_error(error: LiteratureProviderError) -> HTTPException:
    retry_after = error.retry_after_seconds
    detail = {
        "code": error.code.value,
        "message": _PROVIDER_MESSAGES[error.code],
        "provider": error.provider,
        "operation": error.operation.value,
        "retryable": error.retryable,
        "retry_after_seconds": retry_after,
    }
    headers = None
    if error.code is ProviderErrorCode.RATE_LIMITED and retry_after is not None:
        headers = {"Retry-After": str(max(0, math.ceil(retry_after)))}
    return HTTPException(
        status_code=_PROVIDER_STATUS[error.code],
        detail=detail,
        headers=headers,
    )


def _service_http_error(error: LiteratureServiceError) -> HTTPException:
    return HTTPException(
        status_code=_SERVICE_STATUS[error.code],
        detail={"code": error.code.value, "message": str(error)},
    )


def register_literature_routes(
    app: FastAPI,
    *,
    service: LiteratureService,
) -> dict[str, Any]:
    """Register Literature API routes with an explicitly owned service."""

    @app.get("/api/literature/providers", response_model=LiteratureProvidersResponse)
    def list_literature_providers():
        return {"providers": service.capabilities()}

    @app.post("/api/literature/search", response_model=LiteratureSearchExecution)
    async def search_literature(req: LiteratureSearchRequest):
        try:
            return await service.search(req.provider, req.query, plan=req.plan)
        except LiteratureProviderError as exc:
            raise _provider_http_error(exc) from exc
        except LiteratureServiceError as exc:
            raise _service_http_error(exc) from exc

    @app.get("/api/literature/record", response_model=PaperRecord)
    async def get_literature_record(
        provider: str = Query(min_length=1, max_length=64),
        external_id: str = Query(min_length=1, max_length=500),
    ):
        try:
            return await service.get_record(provider, external_id)
        except LiteratureProviderError as exc:
            raise _provider_http_error(exc) from exc
        except LiteratureServiceError as exc:
            raise _service_http_error(exc) from exc

    @app.post("/api/literature/import", response_model=LiteratureImportBatch)
    async def import_literature(req: LiteratureImportRequest):
        try:
            return await service.import_selection(
                project_path=req.project_path,
                search_execution_id=req.search_execution_id,
                paper_ids=req.paper_ids,
            )
        except LiteratureServiceError as exc:
            raise _service_http_error(exc) from exc

    state = {"service": service, "shutdown": service.aclose}
    app.state._state_literature = state
    return state


__all__ = [
    "LiteratureImportRequest",
    "LiteratureProvidersResponse",
    "LiteratureSearchRequest",
    "ProjectSourceManifestStore",
    "register_literature_routes",
]
