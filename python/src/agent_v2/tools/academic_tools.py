"""学术工具 — 翻译、导出、结构化文献检索与范围内 RAG 检索。

参考 claw-code: retrieve_context_tool (RAG), dispatch_tool (file ops).

A1 (plan 5.11 / D-036): literature access goes through the deterministic
services only, always inside the workspace project, and the confirmed calls use
``approval_scope="exact-input"``.  The old raw-ArXiv tool and the unscoped
``rag_search`` are deliberately gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from src.agent_v2.runtime.file_mutations import atomic_write_bytes, atomic_write_text
from src.agent_v2.tools.registry import ToolRegistry, ToolResult

_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\(([^)\s]+)(?:\s+[^)]*)?\)")
_LATEX_IMAGE_RE = re.compile(r"\\includegraphics(?:\[[^\]]*])?\{([^}]+)}")
_LATEX_BIB_RE = re.compile(r"\\bibliography\{([^}]+)}|\\addbibresource\{([^}]+)}")
_YAML_BIB_RE = re.compile(r"(?im)^\s*bibliography\s*:\s*[\"']?([^\"'\r\n]+\.bib)[\"']?\s*$")
_YAML_TEMPLATE_RE = re.compile(r"(?im)^\s*template\s*:\s*[\"']?([^\"'\r\n#]+?)[\"']?\s*(?:#.*)?$")
_LATEX_CITE_RE = re.compile(r"\\cite\w*\{([^}]+)}")
_PANDOC_CITE_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_:.+\-/]+)")
_BIB_KEY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)", re.IGNORECASE)
_WEB_FETCH_MAX_BYTES = 2 * 1024 * 1024
_ACADEMIC_PAGE_LIMIT = 10


def _academic_unavailable(source_kind: str, query: dict[str, str]) -> dict:
    """Return a successful, complete envelope for an expected missing state."""
    source_version = hashlib.sha256(
        json.dumps(
            {"source_kind": source_kind, "query": query},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "available": False,
        "source_kind": source_kind,
        "query": query,
        "mode": "summary",
        "collection": "items",
        "source_id": "",
        "source_version": source_version,
        "source_doc_hash": None,
        "current_doc_hash": None,
        "stale": None,
        "total_items": 0,
        "counts": {},
        "returned_items": 0,
        "complete": True,
        "next_cursor": None,
        "items": [],
        "available_collections": {},
    }


def _document_hash(
    registry: ToolRegistry, doc_id: str, stored_hash: str | None
) -> tuple[str | None, bool | None]:
    """Return the current document hash and whether persisted academic state is stale."""
    if not doc_id:
        return None, None
    try:
        path = registry._resolve_path(doc_id)
    except (OSError, ValueError):
        return None, None
    if not path.is_file():
        return None, None
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None, None
    current_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
    return current_hash, bool(stored_hash and stored_hash != current_hash)


def _academic_page(
    *,
    registry: ToolRegistry,
    payload: object,
    args: dict,
    primary_collection: str,
    allowed_collections: set[str],
) -> dict:
    """Build a bounded, cursor-addressable envelope for large academic state."""
    if not isinstance(payload, dict):
        payload = {"items": payload if isinstance(payload, list) else [payload]}
        primary_collection = "items"
        allowed_collections = {"items"}

    mode = str(args.get("mode", "summary") or "summary").strip().lower()
    if mode not in {"summary", "detail"}:
        mode = "summary"
    collection = str(args.get("collection", primary_collection) or primary_collection)
    if collection not in allowed_collections:
        collection = primary_collection
    raw_items = payload.get(collection, [])
    items = list(raw_items) if isinstance(raw_items, list) else []
    source_version = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    doc_id = str(payload.get("doc_id", "") or "")
    stored_doc_hash = str(payload.get("doc_hash", "") or "")
    current_doc_hash, stale = _document_hash(registry, doc_id, stored_doc_hash)

    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("status") or item.get("severity") or "unclassified")
        counts[label] = counts.get(label, 0) + 1

    base = {
        "available": True,
        "mode": mode,
        "collection": collection,
        "source_id": str(payload.get("id", "") or ""),
        "source_version": source_version,
        "source_doc_hash": stored_doc_hash or None,
        "current_doc_hash": current_doc_hash,
        "stale": stale,
        "total_items": len(items),
        "counts": counts,
    }
    if mode == "summary":
        detail_available = bool(items)
        return {
            **base,
            "returned_items": 0,
            # A summary is not evidence that every underlying item was read.
            "complete": not detail_available,
            "next_cursor": 0 if detail_available else None,
            "items": [],
            "available_collections": {
                name: len(value) if isinstance(value, list) else 0
                for name, value in payload.items()
                if name in allowed_collections
            },
        }

    requested_ids = args.get("item_ids", [])
    item_ids = {
        str(value)
        for value in requested_ids
        if isinstance(requested_ids, list) and str(value).strip()
    }
    if item_ids:
        items = [
            item for item in items if isinstance(item, dict) and str(item.get("id", "")) in item_ids
        ]
        cursor = 0
    else:
        try:
            cursor = max(0, int(args.get("cursor", 0) or 0))
        except (TypeError, ValueError):
            cursor = 0
    try:
        limit = max(1, min(_ACADEMIC_PAGE_LIMIT, int(args.get("limit", 5) or 5)))
    except (TypeError, ValueError):
        limit = 5
    page = items[cursor : cursor + limit]
    next_cursor = None if cursor + len(page) >= len(items) else cursor + len(page)
    return {
        **base,
        "total_items": len(items),
        "returned_items": len(page),
        "complete": next_cursor is None,
        "next_cursor": next_cursor,
        "items": page,
    }


def _local_resource_path(source: Path, raw_path: str) -> Path | None:
    cleaned = raw_path.strip().strip("\"'")
    if not cleaned or cleaned.startswith(("#", "data:")):
        return None
    parsed = urlsplit(cleaned)
    if parsed.scheme or parsed.netloc:
        return None
    return (source.parent / parsed.path).resolve()


def _resource_is_in_workspace(resource: Path, workspace_root: Path) -> bool:
    try:
        resource.relative_to(workspace_root.resolve())
        return True
    except ValueError:
        return False


def _preflight_document_resources(
    source: Path,
    content: str,
    workspace_root: Path,
) -> list[str]:
    """Return deterministic missing-resource diagnostics before export."""

    issues: list[str] = []
    image_refs = [match.group(1) for match in _MARKDOWN_IMAGE_RE.finditer(content)]
    image_refs.extend(match.group(1) for match in _LATEX_IMAGE_RE.finditer(content))
    for raw_path in image_refs:
        resource = _local_resource_path(source, raw_path)
        if resource is not None and not _resource_is_in_workspace(resource, workspace_root):
            issues.append(f"image escapes workspace: {raw_path}")
        elif resource is not None and not resource.is_file():
            issues.append(f"missing image: {raw_path}")

    template_refs = [match.group(1).strip() for match in _YAML_TEMPLATE_RE.finditer(content)]
    for raw_path in template_refs:
        resource = _local_resource_path(source, raw_path)
        if resource is not None and not _resource_is_in_workspace(resource, workspace_root):
            issues.append(f"template escapes workspace: {raw_path}")
        elif resource is not None and not resource.is_file():
            issues.append(f"missing template: {raw_path}")

    bib_refs: list[str] = []
    for match in _LATEX_BIB_RE.finditer(content):
        raw_group = match.group(1) or match.group(2) or ""
        for item in raw_group.split(","):
            value = item.strip()
            if value and not value.lower().endswith(".bib"):
                value += ".bib"
            if value:
                bib_refs.append(value)
    bib_refs.extend(match.group(1).strip() for match in _YAML_BIB_RE.finditer(content))

    bib_paths: list[Path] = []
    for raw_path in bib_refs:
        resource = _local_resource_path(source, raw_path)
        if resource is None:
            continue
        if not _resource_is_in_workspace(resource, workspace_root):
            issues.append(f"bibliography escapes workspace: {raw_path}")
        elif not resource.is_file():
            issues.append(f"missing bibliography: {raw_path}")
        else:
            bib_paths.append(resource)

    cited_keys: set[str] = set()
    for match in _LATEX_CITE_RE.finditer(content):
        cited_keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    cited_keys.update(_PANDOC_CITE_RE.findall(content))
    if cited_keys:
        if not bib_paths:
            if not bib_refs:
                issues.append("citations found but no bibliography resource is declared")
        else:
            known_keys: set[str] = set()
            for path in bib_paths:
                try:
                    known_keys.update(_BIB_KEY_RE.findall(path.read_text(encoding="utf-8")))
                except OSError as exc:
                    issues.append(f"cannot read bibliography {path.name}: {exc}")
            for key in sorted(cited_keys - known_keys):
                issues.append(f"missing bibliography key: {key}")
    return issues


def _validate_public_http_url(
    url: str,
    *,
    resolved_ips: list[str] | None,
) -> tuple[bool, str]:
    """Reject URLs that can address local, private, or otherwise non-public hosts."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        return False, f"invalid URL: {exc}"
    if parsed.scheme not in {"http", "https"}:
        return False, "URL scheme must be http or https"
    if not parsed.hostname:
        return False, "URL hostname is required"
    if parsed.username or parsed.password:
        return False, "URL credentials are not allowed"
    try:
        port = parsed.port
    except ValueError as exc:
        return False, f"invalid URL port: {exc}"
    expected_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != expected_port:
        return False, f"URL port {port} is not allowed for {parsed.scheme}"
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False, "localhost targets are not allowed"

    addresses = list(resolved_ips or [])
    with suppress(ValueError):
        addresses.append(str(ipaddress.ip_address(hostname)))
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False, f"invalid resolved IP address: {address}"
        if not ip.is_global:
            return False, f"non-public network target is not allowed: {ip}"
    return True, ""


async def _resolve_public_http_target(url: str) -> tuple[bool, str, str | None]:
    allowed, reason = _validate_public_http_url(url, resolved_ips=None)
    if not allowed:
        return allowed, reason, None
    hostname = urlsplit(url).hostname
    if hostname is None:
        return False, "URL hostname is required", None
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        return False, f"hostname resolution failed: {exc}", None
    addresses = sorted(
        {str(info[4][0]) for info in infos},
        key=lambda value: (
            ipaddress.ip_address(value).version,
            int(ipaddress.ip_address(value)),
        ),
    )
    if not addresses:
        return False, "hostname did not resolve to an address", None
    allowed, reason = _validate_public_http_url(url, resolved_ips=addresses)
    return allowed, reason, addresses[0] if allowed else None


async def _resolve_public_http_url(url: str) -> tuple[bool, str]:
    """Compatibility wrapper used by validation tests and callers."""
    allowed, reason, _address = await _resolve_public_http_target(url)
    return allowed, reason


def _pinned_http_request_parts(url: str, address: str) -> tuple[str, str, str]:
    """Build an IP-pinned URL while preserving the HTTP and TLS host identity."""
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("URL hostname is required")
    pinned_host = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
    pinned_url = urlunsplit((parsed.scheme, pinned_host, parsed.path or "/", parsed.query, ""))
    return pinned_url, parsed.netloc, hostname


async def _fetch_pinned_public_url(url: str) -> tuple[int, dict[str, str], str] | ToolResult:
    """Fetch one URL by connecting only to a DNS address validated as public."""
    allowed, reason, address = await _resolve_public_http_target(url)
    if not allowed or address is None:
        return ToolResult(f"Fetch blocked: {reason}", is_error=True)

    import httpx

    pinned_url, host_header, sni_hostname = _pinned_http_request_parts(url, address)
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request = client.build_request(
            "GET",
            pinned_url,
            headers={
                "Host": host_header,
                "User-Agent": "ScholarAssistant/0.4",
            },
        )
        request.extensions["sni_hostname"] = sni_hostname
        response = await client.send(request, stream=True)
        try:
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > _WEB_FETCH_MAX_BYTES:
                        return ToolResult(
                            "Fetch blocked: response body is too large", is_error=True
                        )
                except ValueError:
                    pass

            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _WEB_FETCH_MAX_BYTES:
                    return ToolResult("Fetch blocked: response body is too large", is_error=True)
                chunks.append(chunk)
            text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            return response.status_code, dict(response.headers), text
        finally:
            await response.aclose()


def register_academic_tools(registry: ToolRegistry) -> None:
    """注册学术领域工具到 ToolRegistry。"""

    # ---- translate_document ----
    async def translate_document(args: dict) -> ToolResult:
        """翻译文档。调用现有的翻译管道。"""
        file_path = str(args.get("file_path", ""))
        source_lang = str(args.get("source_lang", "en"))
        target_lang = str(args.get("target_lang", "zh-CN"))
        str(args.get("engine", "cloud"))

        if not file_path:
            return ToolResult("error: file_path is required", is_error=True)

        try:
            full = registry._resolve_path(file_path)
        except ValueError as e:
            return ToolResult(f"error: {e}", is_error=True)
        if not full.is_file():
            return ToolResult(f"error: file not found: {file_path}", is_error=True)

        # Use the existing translation pipeline via HTTP call to local API
        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(f"{api_base}/api/translate/path", json={"path": str(full)})
                if resp.status_code != 200:
                    return ToolResult(
                        f"error: translation API returned {resp.status_code}", is_error=True
                    )
                data = resp.json()
                task_id = data.get("task_id", "")
                if not task_id:
                    return ToolResult(
                        f"Translation queued for {file_path} ({source_lang} → {target_lang})"
                    )
                return ToolResult(
                    f"Translation started: {file_path} ({source_lang} → {target_lang}), task_id={task_id}"
                )
        except Exception as e:
            return ToolResult(
                f"error connecting to translation API: {e}. Is the API running on port 18088?",
                is_error=True,
            )

    # ---- export_document ----
    async def export_document(args: dict) -> ToolResult:
        """导出文档为 LaTeX/Word/PDF。"""
        file_path = str(args.get("file_path", ""))
        fmt = str(args.get("format", "latex"))

        if not file_path:
            return ToolResult("error: file_path is required", is_error=True)

        try:
            full = registry._resolve_path(file_path)
        except ValueError as e:
            return ToolResult(f"error: {e}", is_error=True)
        if not full.is_file():
            return ToolResult(f"error: file not found: {file_path}", is_error=True)

        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=120.0) as client:
                markdown = await asyncio.to_thread(full.read_text, encoding="utf-8")
                preflight_issues = _preflight_document_resources(
                    full,
                    markdown,
                    registry._workspace_root or full.parent,
                )
                allow_missing = bool(args.get("allow_missing_resources", False))
                if preflight_issues and not allow_missing:
                    details = "\n".join(f"- {issue}" for issue in preflight_issues)
                    return ToolResult(
                        f"error: document resource preflight failed:\n{details}",
                        is_error=True,
                    )
                normalized = fmt.lower()
                if normalized in ("word", "docx"):
                    resp = await client.post(
                        f"{api_base}/api/export/word",
                        json={"content": markdown, "title": full.stem},
                    )
                elif normalized == "pdf":
                    resp = await client.post(
                        f"{api_base}/api/export/pdf",
                        json={"markdown": markdown, "title": full.stem},
                    )
                else:
                    resp = await client.post(
                        f"{api_base}/api/export",
                        json={"markdown": markdown, "title": full.stem},
                    )
                if resp.status_code != 200:
                    return ToolResult(
                        f"error: export API returned {resp.status_code}", is_error=True
                    )
                if normalized == "pdf":
                    out_path = full.with_suffix(".pdf")
                    await asyncio.to_thread(atomic_write_bytes, out_path, resp.content)
                    message = f"Export successful: {out_path}"
                    if preflight_issues:
                        message += "\nwarning: missing resources were explicitly allowed"
                    return ToolResult(message)
                data = resp.json()
                if normalized in ("word", "docx"):
                    generated_path = data.get("path")
                    if not generated_path or not Path(generated_path).is_file():
                        return ToolResult(
                            "error: Word export did not produce a file", is_error=True
                        )
                    out_path = full.with_suffix(".docx")
                    generated_bytes = await asyncio.to_thread(Path(generated_path).read_bytes)
                    await asyncio.to_thread(atomic_write_bytes, out_path, generated_bytes)
                else:
                    tex = data.get("tex")
                    if not isinstance(tex, str) or not tex:
                        return ToolResult("error: LaTeX export returned no content", is_error=True)
                    out_path = full.with_suffix(".tex")
                    await asyncio.to_thread(atomic_write_text, out_path, tex)
                message = f"Export successful: {out_path}"
                if preflight_issues:
                    message += "\nwarning: missing resources were explicitly allowed"
                return ToolResult(message)
        except Exception as e:
            return ToolResult(f"error connecting to export API: {e}", is_error=True)

    # ---- literature tools (A1, plan 5.11 / decision D-036) ----------------
    #
    # The Agent may only reach the deterministic literature services and always
    # inside the workspace project.  ``project_root`` is taken from the registry
    # workspace and can never be supplied by the caller; the three confirmed
    # calls are registered with ``approval_scope="exact-input"`` so a change of
    # query, selection or scope stops at the existing approval contract.

    def _workspace_project() -> str | ToolResult:
        root = registry._workspace_root
        if root is None:
            return ToolResult(
                "error: project_scope_unavailable — no workspace project is selected",
                is_error=True,
            )
        return str(root)

    def _service_error(response, *, action: str) -> ToolResult:
        code = f"http_{response.status_code}"
        message = ""
        try:
            detail = response.json().get("detail")
        except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
            detail = None
        if isinstance(detail, dict):
            code = str(detail.get("code") or code)
            message = str(detail.get("message") or "")
        elif isinstance(detail, str):
            message = detail
        return ToolResult(
            json.dumps(
                {"error": code, "action": action, "message": message},
                ensure_ascii=False,
            ),
            is_error=True,
        )

    def _error_code_of(response) -> str:
        """Structured error code from a service response, or an HTTP fallback."""

        try:
            detail = response.json().get("detail")
        except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
            return f"http_{response.status_code}"
        if isinstance(detail, dict) and detail.get("code"):
            return str(detail["code"])
        return f"http_{response.status_code}"

    async def _available_providers(client) -> list[str]:
        """Registered literature providers, for an actionable error message."""

        try:
            response = await client.get(f"{_api_base()}/api/literature/providers")
            if response.status_code != 200:
                return []
            providers = response.json().get("providers") or []
        except Exception:  # noqa: BLE001 - the hint is optional
            return []
        return [str(item.get("provider")) for item in providers if item.get("provider")]

    def _api_base() -> str:
        return os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")

    async def _post_service(client, path: str, payload: dict):
        api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
        return await client.post(f"{api_base}{path}", json=payload)

    def _access_state(record: dict) -> str:
        locations = record.get("access_locations") or []
        if any(location.get("access_status") == "open" for location in locations):
            return "open"
        if locations:
            return "restricted"
        return "unknown"

    async def literature_providers(args: dict) -> ToolResult:
        """List registered literature providers so a model need not guess names."""

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{os.environ.get('SCHOLAR_API_BASE', 'http://localhost:18088')}"
                    "/api/literature/providers"
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_providers")
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - report, never silently degrade
            return ToolResult(f"literature providers lookup failed: {exc}", is_error=True)

        providers = [
            {
                "provider": item.get("provider"),
                "result_mode": item.get("result_mode"),
                "supports_search": item.get("supports_search"),
            }
            for item in (payload.get("providers") or [])
        ]
        return ToolResult(
            json.dumps(
                {
                    "provider_count": len(providers),
                    "providers": providers,
                    "note": (
                        "'fixture' serves the offline demo corpus; 'arxiv' is the live "
                        "public API. Search results must never be relabelled across modes."
                    ),
                },
                ensure_ascii=False,
            )
        )

    async def literature_search(args: dict) -> ToolResult:
        """Run a confirmed query against the structured literature service."""
        provider = str(args.get("provider", "")).strip()
        query = str(args.get("query", "")).strip()
        research_question = str(args.get("research_question", "")).strip() or query
        max_results = max(1, min(int(args.get("max_results", 5)), 20))
        if not provider:
            return ToolResult("error: provider is required", is_error=True)
        if not query:
            return ToolResult("error: query is required", is_error=True)

        try:
            import httpx

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await _post_service(
                    client,
                    "/api/literature/search",
                    {
                        "provider": provider,
                        # The query is the confirmed expression, so the plan
                        # records it as user-supplied rather than generated.
                        "plan": {
                            "research_question": research_question,
                            "suggested_query": query,
                            "generation_method": "user",
                            "generation_model": None,
                            "generation_config": {"source": "agent_tool"},
                        },
                        "query": {
                            "query": query,
                            "page": 1,
                            "page_size": max_results,
                            "sort_by": "relevance",
                            "sort_order": "descending",
                            "filters": {"year_from": None, "year_to": None, "categories": []},
                        },
                    },
                )
                if response.status_code != 200:
                    error = _service_error(response, action="literature_search")
                    # A model that invents a provider name must be told which ones
                    # exist, otherwise it burns the turn guessing (observed with a
                    # real model asking for "semantic_scholar").
                    if _error_code_of(response) == "provider_not_found":
                        available = await _available_providers(client)
                        if available:
                            error = ToolResult(
                                f"{error.output}\navailable providers: {', '.join(available)}",
                                is_error=True,
                            )
                    return error
                execution = response.json()
        except Exception as exc:  # noqa: BLE001 - report, never silently degrade
            return ToolResult(f"literature search failed: {exc}", is_error=True)

        page = execution.get("page") or {}
        records = page.get("records") or []
        payload = {
            "provider": page.get("provider"),
            "result_mode": page.get("result_mode"),
            "confirmed_query": query,
            "search_execution_id": execution.get("search_execution_id"),
            "total_results": page.get("total_results"),
            "records": [
                {
                    "paper_id": record.get("paper_id"),
                    "title": record.get("title"),
                    "authors": list(record.get("authors") or [])[:5],
                    "year": record.get("year"),
                    "access": _access_state(record),
                    "record_url": record.get("record_url"),
                }
                for record in records[:max_results]
            ],
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            metadata={
                "source_kind": "literature_search",
                "provider": payload["provider"],
                "result_mode": payload["result_mode"],
                "query": query,
                "search_execution_id": payload["search_execution_id"],
                "record_count": len(payload["records"]),
            },
        )

    async def literature_import(args: dict) -> ToolResult:
        """Add confirmed search results to the workspace project library."""

        execution_id = str(args.get("search_execution_id", "")).strip()
        raw_paper_ids = args.get("paper_ids") or []
        paper_ids = [str(item).strip() for item in raw_paper_ids if str(item).strip()]
        if not execution_id:
            return ToolResult("error: search_execution_id is required", is_error=True)
        if not paper_ids:
            return ToolResult("error: paper_ids must not be empty", is_error=True)

        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project
        try:
            import httpx

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await _post_service(
                    client,
                    "/api/literature/import",
                    {
                        "project_path": project,
                        "search_execution_id": execution_id,
                        "paper_ids": paper_ids,
                    },
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_import")
                batch = response.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"literature import failed: {exc}", is_error=True)

        payload = {
            "created_count": batch.get("created_count", 0),
            "reused_count": batch.get("reused_count", 0),
            "metadata_updated_count": batch.get("metadata_updated_count", 0),
            "search_execution_id": execution_id,
            "paper_ids": paper_ids,
            "sources": [
                {
                    "paper_id": item.get("paper_id"),
                    "source_id": item.get("source_id"),
                    "disposition": item.get("disposition"),
                }
                for item in batch.get("results") or []
            ],
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            metadata={
                "source_kind": "literature_import",
                "created_count": payload["created_count"],
                "reused_count": payload["reused_count"],
            },
        )

    async def literature_acquire_fulltext(args: dict) -> ToolResult:
        """Ask the service for the declared open full text of one source."""

        source_id = str(args.get("source_id", "")).strip()
        force = bool(args.get("force", False))
        if not source_id:
            return ToolResult("error: source_id is required", is_error=True)
        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project
        try:
            import httpx

            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await _post_service(
                    client,
                    "/api/literature/fulltext",
                    {"project_path": project, "source_id": source_id, "force": force},
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_acquire_fulltext")
                result = response.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"full text acquisition failed: {exc}", is_error=True)

        payload = {
            "source_id": result.get("source_id"),
            "status": result.get("status"),
            "reused": result.get("reused"),
            "has_local_path": bool(result.get("local_path")),
            "failure_reason": result.get("failure_reason"),
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            metadata={
                "source_kind": "literature_acquire_fulltext",
                "status": payload["status"],
            },
        )

    async def literature_index(args: dict) -> ToolResult:
        """Build the page-level evidence index for one source that has full text."""

        source_id = str(args.get("source_id", "")).strip()
        force = bool(args.get("force", False))
        if not source_id:
            return ToolResult("error: source_id is required", is_error=True)
        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project
        try:
            import httpx

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await _post_service(
                    client,
                    "/api/literature/index",
                    {"project_path": project, "source_id": source_id, "force": force},
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_index")
                result = response.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"literature indexing failed: {exc}", is_error=True)

        payload = {
            "source_id": result.get("source_id"),
            "status": result.get("status"),
            "reused": result.get("reused"),
            "chunk_count": result.get("chunk_count"),
            "page_count": result.get("page_count"),
            "artifact_sha256": result.get("artifact_sha256"),
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            metadata={
                "source_kind": "literature_index",
                "status": payload["status"],
                "reused": payload["reused"],
                "chunk_count": payload["chunk_count"],
            },
        )

    async def literature_sources(args: dict) -> ToolResult:
        """List the current project's literature scope and its index state.

        The Agent needs this to plan inside one project and to skip work that is
        already done (``already_indexed``), instead of re-indexing or guessing
        which sources exist.
        """

        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project
        limit = max(1, min(int(args.get("limit", 50)), 200))
        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{api_base}/api/project/sources",
                    params={"project_path": project},
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_sources")
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"literature sources lookup failed: {exc}", is_error=True)

        sources = []
        for source in (payload.get("sources") or [])[:limit]:
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            literature = metadata.get("literature") if isinstance(metadata, dict) else None
            fulltext = literature.get("fulltext") if isinstance(literature, dict) else None
            original_path = source.get("original_path")
            fulltext_status = (fulltext or {}).get("status")
            index_state = literature.get("index") if isinstance(literature, dict) else None
            sources.append(
                {
                    "source_id": source.get("id"),
                    "title": source.get("title"),
                    "year": metadata.get("year"),
                    # ``rag_status`` is the workspace UI field; the authoritative
                    # index state belongs to the literature metadata the service
                    # writes, so "already indexed" is derived from that.
                    "rag_status": source.get("rag_status"),
                    "fulltext_status": fulltext_status,
                    "already_indexed": bool(
                        fulltext_status == "indexed" or isinstance(index_state, dict)
                    ),
                    "has_fulltext": bool(original_path),
                    "is_literature": isinstance(literature, dict),
                    "paper_id": metadata.get("paper_id"),
                }
            )
        result = {
            "project_root": project,
            "source_count": len(sources),
            "indexed_count": sum(1 for item in sources if item["already_indexed"]),
            "sources": sources,
        }
        return ToolResult(
            json.dumps(result, ensure_ascii=False),
            metadata={
                "source_kind": "literature_sources",
                "source_count": result["source_count"],
                "indexed_count": result["indexed_count"],
            },
        )

    async def literature_answer(args: dict) -> ToolResult:
        """Answer a question from evidence scoped to the selected project sources."""

        question = str(args.get("question", "")).strip()
        raw_source_ids = args.get("source_ids") or []
        source_ids = [str(item).strip() for item in raw_source_ids if str(item).strip()]
        top_k = max(1, min(int(args.get("top_k", 5)), 20))
        if not question:
            return ToolResult("error: question is required", is_error=True)
        if not source_ids:
            return ToolResult(
                "error: source_ids is required — an unscoped answer is not allowed; "
                "select the project sources first",
                is_error=True,
            )

        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project
        try:
            import httpx

            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await _post_service(
                    client,
                    "/api/literature/answer",
                    {
                        "project_path": project,
                        "question": question,
                        "source_ids": source_ids,
                        "top_k": top_k,
                    },
                )
                if response.status_code != 200:
                    return _service_error(response, action="literature_answer")
                answer = response.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"literature answer failed: {exc}", is_error=True)

        evidence: list[dict] = []
        for item in (answer.get("evidence") or [])[:20]:
            span = item.get("span") or {}
            evidence.append(
                {
                    "evidence_id": span.get("evidence_id"),
                    "source_id": item.get("source_id"),
                    "title": item.get("title"),
                    "page": span.get("page_start"),
                    "chunk_id": item.get("chunk_id") or span.get("chunk_id"),
                    "artifact_sha256": span.get("artifact_sha256"),
                    "exact_quote": span.get("exact_quote"),
                    "context_before": span.get("context_before") or "",
                    "context_after": span.get("context_after") or "",
                }
            )
        payload = {
            "status": answer.get("status"),
            "insufficient_reason": answer.get("insufficient_reason"),
            # Echo the submitted scope so the session record stays reviewable even
            # when the session runs with auto-approval enabled.
            "question": question,
            "source_ids": source_ids,
            "claims": [
                {
                    # The claim id is the same value the plain service entry
                    # returns, so a session record can be compared with it.
                    "claim_id": claim.get("claim_id"),
                    "text": claim.get("text"),
                    "evidence_ids": list(claim.get("evidence_ids") or []),
                    "evidence_status": claim.get("evidence_status"),
                }
                for claim in answer.get("claims") or []
            ],
            "evidence": evidence,
            "rejected_claims": [
                {"text": item.get("text"), "reason": item.get("reason")}
                for item in answer.get("rejected_claims") or []
            ],
            "unresolved_count": len(answer.get("unresolved") or []),
            "model": {
                "provider": answer.get("model_provider"),
                "name": answer.get("model_name"),
                "config_hash": answer.get("model_config_hash"),
            },
        }
        if payload["status"] == "insufficient":
            # Plan 5.12 rule 3: an insufficiency must change the next step rather
            # than invite an answer from memory.
            payload["next_actions"] = [
                "widen_scope_within_project",
                "propose_new_query_for_confirmation",
            ]
            payload["must_not_answer_from_memory"] = True
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            metadata={
                "source_kind": "literature_answer",
                "status": payload["status"],
                "insufficient_reason": payload["insufficient_reason"],
                "claim_count": len(payload["claims"]),
                "evidence_count": len(evidence),
            },
        )

    # Register tools
    registry.register(
        "translate_document",
        "Translate a PDF or Markdown document",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the document"},
                "source_lang": {"type": "string", "default": "en"},
                "target_lang": {"type": "string", "default": "zh-CN"},
                "engine": {"type": "string", "default": "cloud"},
            },
            "required": ["file_path"],
        },
        translate_document,
        permission="read-only",
        effects={"external_side_effect", "cost"},
        approval_scope="exact-input",
        network_scope={"local-translation-api"},
    )

    registry.register(
        "export_document",
        "Export document to LaTeX, Word, or PDF",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the document"},
                "format": {
                    "type": "string",
                    "default": "latex",
                    "description": "latex, docx, or pdf",
                },
                "allow_missing_resources": {
                    "type": "boolean",
                    "default": False,
                    "description": "Explicitly allow export despite missing local resources",
                },
            },
            "required": ["file_path"],
        },
        export_document,
        permission="workspace-write",
        effects={"filesystem_write", "network"},
        approval_scope="path",
        network_scope={"local-export-api"},
        rollback_capability="journaled",
    )

    registry.register(
        "literature_search",
        "Search structured literature. Requires the exact search query the user "
        "confirmed; returns normalized records with their access state. Call "
        "literature_sources first to reuse an existing project scope.",
        {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": (
                        "Registered literature provider name: 'arxiv' for the live "
                        "public API, 'fixture' for the offline demo corpus. Use "
                        "literature_providers to list what is registered instead of "
                        "guessing a name."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "The confirmed provider-native search expression",
                },
                "research_question": {
                    "type": "string",
                    "description": "The research question this query was confirmed for",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
            "required": ["provider", "query"],
        },
        literature_search,
        permission="read-only",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-literature-api"},
    )

    registry.register(
        "literature_providers",
        "List the literature providers this installation has registered, with their "
        "result mode. Call this before literature_search instead of guessing a "
        "provider name.",
        {"type": "object", "properties": {}},
        literature_providers,
        permission="read-only",
    )

    registry.register(
        "literature_sources",
        "List the current project's literature sources with their index and full-text "
        "state. Call this before searching or indexing so already indexed sources are "
        "not processed twice.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
        literature_sources,
        permission="read-only",
    )

    registry.register(
        "literature_acquire_fulltext",
        "Fetch the open full text the provider declared for one project source. "
        "When it reports access_unavailable or acquire_failed, tell the user to "
        "attach a local PDF instead of pretending the paper was downloaded.",
        {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["source_id"],
        },
        literature_acquire_fulltext,
        permission="workspace-write",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-literature-api"},
    )

    registry.register(
        "literature_index",
        "Build or rebuild the page-level evidence index for one source that already "
        "has full text. Check literature_sources first: a source with "
        "already_indexed=true must not be indexed again unless the user asks.",
        {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["source_id"],
        },
        literature_index,
        permission="workspace-write",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-literature-api"},
    )

    registry.register(
        "literature_import",
        "Add confirmed search results to the current project library. Requires "
        "the search_execution_id of the confirmed search.",
        {
            "type": "object",
            "properties": {
                "search_execution_id": {"type": "string"},
                "paper_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
            },
            "required": ["search_execution_id", "paper_ids"],
        },
        literature_import,
        permission="workspace-write",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-literature-api"},
    )

    registry.register(
        "literature_answer",
        "Answer a research question from evidence in the selected project sources. "
        "Requires an explicit source_ids scope; every claim comes back with its "
        "page and exact quote. When the result says insufficient, do NOT answer "
        "from memory or general knowledge: either widen the scope inside the "
        "project or propose a new search query for the user to confirm.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["question", "source_ids"],
        },
        literature_answer,
        permission="read-only",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-literature-api"},
        # A multi-paper answer carries claims plus their page-level evidence;
        # the default 4000-character budget would cut it into invalid JSON.
        max_output_chars=24_000,
    )

    # ---- rag_search — 参考 claw-code retrieve_context_tool ----
    async def rag_search(args: dict) -> ToolResult:
        """范围内检索扁平文本库；无 source_ids 时显式拒绝。"""

        query = str(args.get("query", ""))
        top_k = int(args.get("top_k", 5))
        raw_source_ids = args.get("source_ids") or []
        source_ids = [str(item).strip() for item in raw_source_ids if str(item).strip()]

        if not query:
            return ToolResult("error: query is required", is_error=True)
        if not source_ids:
            return ToolResult(
                "error: source_ids is required — an unscoped library search is not "
                "allowed; use literature_search and literature_answer for project "
                "scoped work",
                is_error=True,
            )
        project = _workspace_project()
        if isinstance(project, ToolResult):
            return project

        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{api_base}/api/rag/query",
                    json={
                        "query": query,
                        "top_k": min(top_k, 10),
                        "project_root": project,
                        "source_ids": source_ids,
                        "project_scoped": True,
                    },
                )
                if resp.status_code == 404:
                    return ToolResult(
                        "RAG not configured. Ingest documents first via the Docs panel."
                    )
                if resp.status_code != 200:
                    return ToolResult(f"RAG query returned {resp.status_code}", is_error=True)
                data = resp.json()
                hits = data.get("hits", data.get("results", []))
                if not hits:
                    return ToolResult("No relevant documents found.")
                lines = []
                for i, hit in enumerate(hits[:top_k]):
                    src = hit.get("source", hit.get("path", hit.get("doc_id", f"doc_{i}")))
                    snippet = hit.get("snippet", hit.get("text", hit.get("content", "")))
                    lines.append(f"[{i + 1}] {src}\n{snippet[:300]}")
                return ToolResult("\n\n".join(lines))
        except Exception as e:
            return ToolResult(f"RAG query failed: {e}", is_error=True)

    registry.register(
        "rag_search",
        (
            "Search the flat-text document library (RAG) inside one project and an "
            "explicit set of source ids. Scoped search only: pass the project sources "
            "you mean to search."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Project source ids this search is limited to",
                },
                "top_k": {"type": "integer", "default": 5, "description": "Number of results"},
            },
            "required": ["query", "source_ids"],
        },
        rag_search,
        permission="read-only",
        effects={"network"},
        approval_scope="exact-input",
        network_scope={"local-rag-api"},
    )

    # ---- Argument Companion / Reviewer read tools -----------------------
    # These tools expose the existing production stores through their public
    # API. They do not duplicate Reviewer-2, Claim Ledger, or Argument Map
    # logic; the Agent receives the same persisted data as the visible panels.

    async def read_argument_graph(args: dict) -> ToolResult:
        graph_id = str(args.get("graph_id", "")).strip()
        source_doc = str(args.get("source_doc", "")).strip()
        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=20.0) as client:
                if graph_id:
                    resp = await client.get(f"{api_base}/api/argument/graph/{graph_id}")
                    if resp.status_code == 404:
                        envelope = _academic_unavailable(
                            "argument_graph", {"graph_id": graph_id, "source_doc": source_doc}
                        )
                        return ToolResult(
                            json.dumps(envelope, ensure_ascii=False),
                            metadata={"available": False, "complete": True, "stale": None},
                        )
                    resp.raise_for_status()
                    return ToolResult(json.dumps(resp.json(), ensure_ascii=False))

                resp = await client.get(f"{api_base}/api/argument/graphs")
                resp.raise_for_status()
                graphs = resp.json()
                if source_doc:
                    normalized = source_doc.replace("\\", "/").lower()
                    graphs = [
                        graph
                        for graph in graphs
                        if str(graph.get("source_doc", "")).replace("\\", "/").lower() == normalized
                    ]
                if not graphs:
                    envelope = _academic_unavailable(
                        "argument_graph", {"graph_id": graph_id, "source_doc": source_doc}
                    )
                    return ToolResult(
                        json.dumps(envelope, ensure_ascii=False),
                        metadata={"available": False, "complete": True, "stale": None},
                    )
                return ToolResult(json.dumps(graphs, ensure_ascii=False))
        except Exception as e:
            return ToolResult(f"Argument graph lookup failed: {e}", is_error=True)

    registry.register(
        "read_argument_graph",
        (
            "Read the real Toulmin argument map. Provide graph_id for one full graph, "
            "or source_doc to find graphs linked to a manuscript."
        ),
        {
            "type": "object",
            "properties": {
                "graph_id": {"type": "string", "description": "Argument graph ID"},
                "source_doc": {"type": "string", "description": "Workspace document path"},
            },
        },
        read_argument_graph,
        permission="read-only",
    )

    async def read_argument_ledger(args: dict) -> ToolResult:
        doc_id = str(args.get("doc_id", "")).strip()
        if not doc_id:
            return ToolResult("error: doc_id is required", is_error=True)
        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=20.0) as client:
                # doc_id remains a query parameter because it may be a full path.
                resp = await client.get(
                    f"{api_base}/api/companion/ledger", params={"doc_id": doc_id}
                )
                if resp.status_code == 404:
                    return ToolResult(f"Claim Ledger not found for: {doc_id}", is_error=True)
                resp.raise_for_status()
                envelope = _academic_page(
                    registry=registry,
                    payload=resp.json(),
                    args=args,
                    primary_collection="promises",
                    allowed_collections={"promises", "anchors"},
                )
                return ToolResult(
                    json.dumps(envelope, ensure_ascii=False),
                    metadata={
                        "complete": envelope["complete"],
                        "next_cursor": envelope["next_cursor"],
                        "source_version": envelope["source_version"],
                        "stale": envelope["stale"],
                    },
                )
        except Exception as e:
            return ToolResult(f"Claim Ledger lookup failed: {e}", is_error=True)

    registry.register(
        "read_argument_ledger",
        (
            "Read the real Claim Ledger through a completeness envelope. The default summary "
            "returns counts and source integrity metadata; use mode=detail with cursor/limit or "
            "item_ids until complete=true. Never describe stale=true or complete=false data as current "
            "or complete."
        ),
        {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Document ID or full workspace file path",
                },
                "mode": {
                    "type": "string",
                    "enum": ["summary", "detail"],
                    "default": "summary",
                },
                "collection": {
                    "type": "string",
                    "enum": ["promises", "anchors"],
                    "default": "promises",
                },
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _ACADEMIC_PAGE_LIMIT,
                    "default": 5,
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _ACADEMIC_PAGE_LIMIT,
                },
            },
            "required": ["doc_id"],
        },
        read_argument_ledger,
        permission="read-only",
    )

    async def read_reviewer_state(args: dict) -> ToolResult:
        session_id = str(args.get("session_id", "")).strip()
        doc_id = str(args.get("doc_id", "")).strip()
        if not session_id and not doc_id:
            return ToolResult("error: session_id or doc_id is required", is_error=True)
        try:
            import httpx

            api_base = os.environ.get("SCHOLAR_API_BASE", "http://localhost:18088")
            async with httpx.AsyncClient(timeout=20.0) as client:
                if session_id:
                    resp = await client.get(f"{api_base}/api/companion/review/{session_id}")
                else:
                    resp = await client.get(
                        f"{api_base}/api/companion/reviews", params={"doc_id": doc_id}
                    )
                if resp.status_code == 404:
                    envelope = _academic_unavailable(
                        "reviewer_state",
                        {"session_id": session_id, "doc_id": doc_id},
                    )
                    return ToolResult(
                        json.dumps(envelope, ensure_ascii=False),
                        metadata={
                            "available": False,
                            "complete": True,
                            "next_cursor": None,
                            "source_version": envelope["source_version"],
                            "stale": None,
                        },
                    )
                resp.raise_for_status()
                payload = resp.json()
                if not session_id and isinstance(payload, list) and len(payload) == 1:
                    resolved_session_id = str(payload[0].get("session_id", "")).strip()
                    if resolved_session_id:
                        detail_resp = await client.get(
                            f"{api_base}/api/companion/review/{resolved_session_id}"
                        )
                        if detail_resp.status_code == 404:
                            envelope = _academic_unavailable(
                                "reviewer_state",
                                {"session_id": resolved_session_id, "doc_id": doc_id},
                            )
                            return ToolResult(
                                json.dumps(envelope, ensure_ascii=False),
                                metadata={
                                    "available": False,
                                    "complete": True,
                                    "next_cursor": None,
                                    "source_version": envelope["source_version"],
                                    "stale": None,
                                },
                            )
                        detail_resp.raise_for_status()
                        payload = detail_resp.json()
                if payload in (None, [], {}) or (
                    isinstance(payload, dict)
                    and not any(
                        isinstance(payload.get(name), list) and payload.get(name)
                        for name in ("points", "anchors", "items")
                    )
                ):
                    envelope = _academic_unavailable(
                        "reviewer_state",
                        {"session_id": session_id, "doc_id": doc_id},
                    )
                    return ToolResult(
                        json.dumps(envelope, ensure_ascii=False),
                        metadata={
                            "available": False,
                            "complete": True,
                            "next_cursor": None,
                            "source_version": envelope["source_version"],
                            "stale": None,
                        },
                    )
                # A doc_id query returns compact review summaries rather than one
                # ReviewSession. Keep them pageable under the same envelope.
                primary = "points" if isinstance(payload, dict) else "items"
                envelope = _academic_page(
                    registry=registry,
                    payload=payload,
                    args=args,
                    primary_collection=primary,
                    allowed_collections={"points", "anchors", "items"},
                )
                return ToolResult(
                    json.dumps(envelope, ensure_ascii=False),
                    metadata={
                        "complete": envelope["complete"],
                        "available": envelope["available"],
                        "next_cursor": envelope["next_cursor"],
                        "source_version": envelope["source_version"],
                        "stale": envelope["stale"],
                    },
                )
        except Exception as e:
            return ToolResult(f"Reviewer-2 lookup failed: {e}", is_error=True)

    registry.register(
        "read_reviewer_state",
        (
            "Read persisted Reviewer-2 state through a completeness envelope. Start with summary, "
            "then use mode=detail with cursor/limit or item_ids until complete=true. Query by doc_id "
            "unless the user or a prior tool result supplied an exact session_id; never invent a "
            "placeholder session ID. available=false is a successful, complete absence result, not "
            "a tool failure. Propagate unavailable, stale, and incomplete status into the final "
            "assessment."
        ),
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Reviewer session ID"},
                "doc_id": {
                    "type": "string",
                    "description": "Document ID or full workspace file path",
                },
                "mode": {
                    "type": "string",
                    "enum": ["summary", "detail"],
                    "default": "summary",
                },
                "collection": {
                    "type": "string",
                    "enum": ["points", "anchors", "items"],
                    "default": "points",
                },
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _ACADEMIC_PAGE_LIMIT,
                    "default": 5,
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _ACADEMIC_PAGE_LIMIT,
                },
            },
        },
        read_reviewer_state,
        permission="read-only",
    )

    # ---- web_search (参考 claw-code WebSearch) ----
    async def web_search(args: dict) -> ToolResult:
        """搜索网页。使用 DuckDuckGo HTML 搜索。"""
        query = str(args.get("query", ""))
        max_results = int(args.get("max_results", 5))

        if not query:
            return ToolResult("error: query is required", is_error=True)

        try:
            from urllib.parse import quote

            import httpx

            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            headers = {"User-Agent": "ScholarAssistant/0.4"}
            async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return ToolResult(f"Search returned {resp.status_code}", is_error=True)
                text = resp.text
                # Simple extraction of result snippets
                import re

                snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.DOTALL)
                results = []
                for s in snippets[:max_results]:
                    cleaned = re.sub(r"<[^>]+>", "", s).strip()
                    if cleaned and len(cleaned) > 10:
                        results.append(cleaned[:300])
                if not results:
                    return ToolResult("No results found.")
                return ToolResult("\n\n".join(f"[{i + 1}] {r}" for i, r in enumerate(results)))
        except Exception as e:
            return ToolResult(f"Search failed: {e}", is_error=True)

    registry.register(
        "web_search",
        "Search the web for information",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        web_search,
        permission="read-only",
        effects={"network"},
        approval_scope="domain",
        network_scope={"html.duckduckgo.com"},
    )

    # ---- web_fetch (参考 claw-code WebFetch) ----
    async def web_fetch(args: dict) -> ToolResult:
        """抓取网页内容。"""
        url = str(args.get("url", ""))
        if not url:
            return ToolResult("error: url is required", is_error=True)
        try:
            current_url = url
            response: tuple[int, dict[str, str], str] | None = None
            for _hop in range(6):
                fetched = await _fetch_pinned_public_url(current_url)
                if isinstance(fetched, ToolResult):
                    return fetched
                response = fetched
                status_code, headers, _text = response
                if status_code not in {301, 302, 303, 307, 308}:
                    break
                location = headers.get("location", "")
                if not location:
                    return ToolResult("Fetch redirect missing Location header", is_error=True)
                current_url = urljoin(current_url, location)
            else:
                return ToolResult("Fetch blocked: too many redirects", is_error=True)
            if response is None:
                return ToolResult("Fetch failed: no response", is_error=True)
            status_code, _headers, text = response
            if status_code != 200:
                return ToolResult(f"Fetch returned {status_code}", is_error=True)

            # Strip HTML tags for plain text
            cleaned = re.sub(
                r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE
            )
            cleaned = re.sub(
                r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE
            )
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) > 5000:
                cleaned = cleaned[:5000] + "... [truncated]"
            return ToolResult(cleaned or "(empty page)")
        except Exception as e:
            return ToolResult(f"Fetch failed: {e}", is_error=True)

    registry.register(
        "web_fetch",
        "Fetch and read the content of a web page",
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch (must start with http:// or https://)",
                },
            },
            "required": ["url"],
        },
        web_fetch,
        permission="read-only",
        effects={"network"},
        approval_scope="domain",
        network_scope={"user-approved-domain"},
    )
