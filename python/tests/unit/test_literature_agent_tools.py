"""A1 tool-layer contract tests (plan 5.11, decision D-036).

The Agent may only reach the deterministic literature services, always inside the
workspace project, and only through calls the user has confirmed.  These tests pin
that contract without any network or model: a fake HTTP client records exactly
what each tool sent and returns canned service payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.agent_v2.tools.academic_tools import register_academic_tools
from src.agent_v2.tools.registry import ToolRegistry

CONFIRMED_QUERY = 'all:"evidence traceable question answering"'
EXECUTION_ID = "search_exec_" + "c" * 32
EVIDENCE_ID = "evidence_" + "a" * 24


class _Response:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> dict:
        return self._data


class _Client:
    def __init__(self, responses: dict[str, _Response], calls: list[tuple[str, dict]]) -> None:
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, *, json: dict):
        self._calls.append((url, json))
        for suffix, response in self._responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected request: {url}")


def build_registry(tmp_path: Path | None = None) -> ToolRegistry:
    registry = ToolRegistry(tmp_path)
    register_academic_tools(registry)
    return registry


def patch_client(monkeypatch: pytest.MonkeyPatch, responses: dict[str, _Response], calls: list):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client(responses, calls))


SEARCH_PAYLOAD = {
    "search_execution_id": EXECUTION_ID,
    "page": {
        "provider": "fixture",
        "result_mode": "fixture",
        "records": [
            {
                "paper_id": "paper_demo_0001",
                "title": "Demo Paper A",
                "authors": ["Demo Author A"],
                "year": 2024,
                "record_url": "https://demo.invalid/records/demo-0001",
                "access_locations": [
                    {"kind": "pdf", "url": "https://demo.invalid/a.pdf", "access_status": "open"}
                ],
            },
            {
                "paper_id": "paper_demo_0002",
                "title": "Demo Paper B",
                "authors": ["Demo Author C"],
                "year": 2025,
                "record_url": "https://demo.invalid/records/demo-0002",
                "access_locations": [],
            },
        ],
        "total_results": 3,
    },
    "plan": {"research_question": "How does it work?"},
}

ANSWER_PAYLOAD = {
    "status": "answered",
    "insufficient_reason": None,
    "claims": [
        {
            "claim_id": "claim_" + "d" * 24,
            "text": "The study evaluates on a held-out split.",
            "evidence_ids": [EVIDENCE_ID],
            "evidence_status": "supported",
        }
    ],
    "evidence": [
        {
            "source_id": "src_demo_0001",
            "doc_id": "project:src_demo_0001",
            "title": "Demo Paper A",
            "chunk_id": "chunk_demo_0001",
            "paper_id": "paper_demo_0001",
            "span": {
                "evidence_id": EVIDENCE_ID,
                "page_start": 2,
                "page_end": 2,
                "exact_quote": "Only this page mentions the held-out split.",
                "context_before": "",
                "context_after": "",
            },
        }
    ],
    "rejected_claims": [],
    "unresolved": [],
    "retrieved_chunk_count": 2,
    "model_provider": "openai",
    "model_name": "gpt-4o",
    "model_config_hash": "e" * 64,
}


class TestToolRegistrationContract:
    def test_raw_arxiv_tool_is_gone(self) -> None:
        registry = build_registry()

        assert registry.get("arxiv_search") is None
        assert registry.get("literature_search") is not None
        assert registry.get("literature_import") is not None
        assert registry.get("literature_answer") is not None

    @pytest.mark.parametrize(
        "tool_name", ["literature_search", "literature_import", "literature_answer"]
    )
    def test_confirmed_calls_use_the_exact_input_approval_scope(self, tool_name: str) -> None:
        spec = build_registry().get(tool_name)

        assert spec is not None
        assert spec.approval_scope == "exact-input"
        assert spec.network_scope is not None
        assert "local-literature-api" in set(spec.network_scope)

    def test_literature_tools_never_ask_for_a_project_path(self) -> None:
        registry = build_registry()

        for tool_name in ("literature_search", "literature_import", "literature_answer"):
            schema = registry.get(tool_name).definition.input_schema
            assert "project_path" not in schema.get("properties", {})
            assert "project_root" not in schema.get("properties", {})


class TestLiteratureSearchTool:
    async def test_search_posts_the_confirmed_query_and_returns_structured_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {"/api/literature/search": _Response(SEARCH_PAYLOAD)}, calls)
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_search",
            {
                "provider": "fixture",
                "query": CONFIRMED_QUERY,
                "research_question": "How does it work?",
                "max_results": 5,
            },
        )

        assert result.is_error is False
        url, body = calls[0]
        assert url.endswith("/api/literature/search")
        assert body["provider"] == "fixture"
        assert body["query"]["query"] == CONFIRMED_QUERY
        assert body["plan"]["suggested_query"] == CONFIRMED_QUERY
        assert body["plan"]["generation_method"] == "user"

        payload = json.loads(result.output)
        assert payload["result_mode"] == "fixture"
        assert payload["search_execution_id"] == EXECUTION_ID
        assert [record["paper_id"] for record in payload["records"]] == [
            "paper_demo_0001",
            "paper_demo_0002",
        ]
        assert payload["records"][0]["access"] == "open"
        assert payload["records"][1]["access"] == "unknown"
        assert payload["total_results"] == 3

    async def test_search_needs_no_workspace_because_it_carries_no_project_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {"/api/literature/search": _Response(SEARCH_PAYLOAD)}, calls)
        registry = build_registry(None)

        result = await registry.execute(
            "literature_search", {"provider": "fixture", "query": CONFIRMED_QUERY}
        )

        assert result.is_error is False
        _, body = calls[0]
        assert "project_path" not in body
        assert "project_root" not in body
        assert "project_path" not in body["query"]

    async def test_search_reports_service_failures_with_their_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(
            monkeypatch,
            {
                "/api/literature/search": _Response(
                    {"detail": {"code": "rate_limited", "message": "slow down"}}, status_code=429
                )
            },
            calls,
        )
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_search", {"provider": "arxiv", "query": CONFIRMED_QUERY}
        )

        assert result.is_error is True
        assert "rate_limited" in result.output


class TestLiteratureImportTool:
    async def test_import_always_uses_the_workspace_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(
            monkeypatch,
            {
                "/api/literature/import": _Response(
                    {
                        "created_count": 1,
                        "reused_count": 1,
                        "results": [
                            {"paper_id": "paper_demo_0001", "source_id": "src_demo_0001"},
                            {"paper_id": "paper_demo_0002", "source_id": "src_demo_0002"},
                        ],
                    }
                )
            },
            calls,
        )
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_import",
            {
                "search_execution_id": EXECUTION_ID,
                "paper_ids": ["paper_demo_0001", "paper_demo_0002"],
                # A caller-supplied project path must not be honoured.
                "project_path": "D:/somewhere/else",
            },
        )

        assert result.is_error is False
        _, body = calls[0]
        assert body["project_path"] == str(tmp_path.resolve())
        assert body["search_execution_id"] == EXECUTION_ID
        assert body["paper_ids"] == ["paper_demo_0001", "paper_demo_0002"]

        payload = json.loads(result.output)
        assert payload["created_count"] == 1
        assert payload["reused_count"] == 1
        assert payload["sources"][0]["source_id"] == "src_demo_0001"

    async def test_import_requires_an_execution_handle_and_a_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(tmp_path)

        missing_ids = await registry.execute(
            "literature_import", {"search_execution_id": EXECUTION_ID, "paper_ids": []}
        )
        assert missing_ids.is_error is True

        missing_execution = await registry.execute(
            "literature_import", {"search_execution_id": "", "paper_ids": ["paper_a"]}
        )
        assert missing_execution.is_error is True
        assert calls == []

    async def test_import_is_refused_without_a_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(None)

        result = await registry.execute(
            "literature_import",
            {"search_execution_id": EXECUTION_ID, "paper_ids": ["paper_demo_0001"]},
        )

        assert result.is_error is True
        assert "project_scope_unavailable" in result.output
        assert calls == []


class TestLiteratureAnswerTool:
    async def test_answer_carries_claims_with_page_level_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {"/api/literature/answer": _Response(ANSWER_PAYLOAD)}, calls)
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_answer",
            {"question": "What protocol?", "source_ids": ["src_demo_0001"]},
        )

        assert result.is_error is False
        _, body = calls[0]
        assert body["project_path"] == str(tmp_path.resolve())
        assert body["question"] == "What protocol?"
        assert body["source_ids"] == ["src_demo_0001"]

        payload = json.loads(result.output)
        assert payload["status"] == "answered"
        assert payload["claims"][0]["evidence_ids"] == [EVIDENCE_ID]
        evidence = payload["evidence"][0]
        assert evidence["page"] == 2
        assert evidence["exact_quote"] == "Only this page mentions the held-out split."
        assert evidence["title"] == "Demo Paper A"

    async def test_answer_reports_insufficiency_without_dressing_it_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(
            monkeypatch,
            {
                "/api/literature/answer": _Response(
                    {
                        **ANSWER_PAYLOAD,
                        "status": "insufficient",
                        "insufficient_reason": "no_resolvable_evidence",
                        "claims": [],
                        "evidence": [],
                    }
                )
            },
            calls,
        )
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_answer", {"question": "Q?", "source_ids": ["src_demo_0001"]}
        )

        assert result.is_error is False
        payload = json.loads(result.output)
        assert payload["status"] == "insufficient"
        assert payload["insufficient_reason"] == "no_resolvable_evidence"
        assert payload["claims"] == []

    async def test_answer_requires_an_explicit_source_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(tmp_path)

        result = await registry.execute("literature_answer", {"question": "Q?", "source_ids": []})

        assert result.is_error is True
        assert "source_ids" in result.output
        assert calls == []

    async def test_answer_requires_a_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_answer", {"question": "  ", "source_ids": ["src_demo_0001"]}
        )

        assert result.is_error is True
        assert calls == []

    async def test_out_of_scope_source_is_reported_as_a_service_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(
            monkeypatch,
            {
                "/api/literature/answer": _Response(
                    {"detail": {"code": "source_not_found", "message": "not in this project"}},
                    status_code=404,
                )
            },
            calls,
        )
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "literature_answer", {"question": "Q?", "source_ids": ["src_other_project"]}
        )

        assert result.is_error is True
        assert "source_not_found" in result.output


class TestRagSearchScope:
    async def test_rag_search_requires_explicit_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(tmp_path)

        result = await registry.execute("rag_search", {"query": "anything"})

        assert result.is_error is True
        assert "source_ids" in result.output
        assert calls == []

    async def test_rag_search_sends_project_and_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(
            monkeypatch,
            {"/api/rag/query": _Response({"hits": [{"chunk_id": "chunk_a", "metadata": {}}]})},
            calls,
        )
        registry = build_registry(tmp_path)

        result = await registry.execute(
            "rag_search", {"query": "anything", "source_ids": ["src_demo_0001"]}
        )

        assert result.is_error is False
        _, body = calls[0]
        assert body["project_root"] == str(tmp_path.resolve())
        assert body["source_ids"] == ["src_demo_0001"]
        assert body["project_scoped"] is True

    async def test_rag_search_is_refused_without_a_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict]] = []
        patch_client(monkeypatch, {}, calls)
        registry = build_registry(None)

        result = await registry.execute(
            "rag_search", {"query": "anything", "source_ids": ["src_demo_0001"]}
        )

        assert result.is_error is True
        assert "project_scope_unavailable" in result.output
        assert calls == []
