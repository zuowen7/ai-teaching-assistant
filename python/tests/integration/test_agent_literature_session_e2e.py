"""A1 session-level acceptance (plan §2.4, §5.11–5.12).

This is the whole A1 loop over the **real** runtime: a scripted model plans the
work, the real ``literature_*`` tools reach the real application, every
side-effecting call stops at the approval contract, and the turn ends with claims
whose evidence can be resolved back to a page quote.

Everything is offline.  The tools talk HTTP to ``SCHOLAR_API_BASE``; here that
address is served by the ASGI application itself through ``httpx.ASGITransport``,
so no server is started and no request leaves the process.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agent_v2.runtime.conversation import ConversationRuntime  # noqa: E402
from src.agent_v2.types import (  # noqa: E402
    AgentEventType,
    ProviderResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

pytestmark = pytest.mark.integration

CONFIG = """\
translator:
  engine: ollama
  model: qwen3:8b
  ollama_base_url: http://localhost:11434
  temperature: 0.3
  timeout: 300.0
chunker:
  max_tokens: 2048
  overlap_tokens: 128
formatter:
  output_format: bilingual
agent:
  model: qwen3:8b
  max_stalled_tool_calls: 12
  max_tool_errors: 5
"""


@pytest.fixture(scope="module")
def demo_helpers():
    module = importlib.import_module("tests.integration.test_literature_demo_e2e")
    return module


class LibraryAgentProvider:
    """Scripted model that plans the A1 chain from the *real* tool results."""

    provider_name = "scripted"
    model = "scripted-library-agent-v1"
    base_url = ""

    def __init__(self, corpus, helpers, *, mode: str = "full_chain") -> None:
        self._corpus = corpus
        self._helpers = helpers
        self.mode = mode
        self.turns = 0

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _results(messages) -> dict[str, list[ToolResultBlock]]:
        by_name: dict[str, list[ToolResultBlock]] = {}
        for message in messages:
            for block in getattr(message, "blocks", []):
                if isinstance(block, ToolResultBlock):
                    by_name.setdefault(block.tool_name, []).append(block)
        return by_name

    @staticmethod
    def _call(name: str, arguments: dict) -> ProviderResponse:
        return ProviderResponse(
            blocks=[
                ToolUseBlock(
                    id=f"call_{name}_{abs(hash(json.dumps(arguments, sort_keys=True))) % 100000}",
                    name=name,
                    input=json.dumps(arguments),
                )
            ]
        )

    @staticmethod
    def _say(text: str) -> ProviderResponse:
        return ProviderResponse(blocks=[TextBlock(text=text)])

    # -- the plan --------------------------------------------------------
    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        self.turns += 1
        by_name = self._results(messages)

        if "literature_sources" not in by_name:
            return self._call("literature_sources", {})

        sources = json.loads(by_name["literature_sources"][-1].output)

        if self.mode == "reuse_existing" and sources["indexed_count"]:
            # Already indexed work must not be processed twice.
            return self._answer(by_name, sources, reuse=True)

        if "literature_search" not in by_name:
            return self._call(
                "literature_search",
                {
                    "provider": "arxiv",
                    "query": self._corpus.confirmed_query,
                    "research_question": self._corpus.question,
                },
            )

        search = json.loads(by_name["literature_search"][-1].output)
        if "literature_import" not in by_name:
            return self._call(
                "literature_import",
                {
                    "search_execution_id": search["search_execution_id"],
                    "paper_ids": [record["paper_id"] for record in search["records"]],
                },
            )

        imported = json.loads(by_name["literature_import"][-1].output)
        source_ids = [item["source_id"] for item in imported["sources"]]

        acquired = by_name.get("literature_acquire_fulltext", [])
        if len(acquired) < len(source_ids):
            return self._call(
                "literature_acquire_fulltext", {"source_id": source_ids[len(acquired)]}
            )

        indexed = by_name.get("literature_index", [])
        if len(indexed) < len(source_ids):
            return self._call("literature_index", {"source_id": source_ids[len(indexed)]})

        return self._answer(by_name, sources, source_ids=source_ids)

    def _answer(self, by_name, sources, *, source_ids=None, reuse=False):
        if "literature_answer" in by_name:
            answer = json.loads(by_name["literature_answer"][-1].output)
            if answer.get("status") == "insufficient":
                return self._say(
                    "证据不足：" + str(answer.get("insufficient_reason")) + "，我不会凭记忆回答。"
                )
            evidence_ids = [item["evidence_id"] for item in answer["evidence"]]
            return self._say(
                f"根据 {len(answer['claims'])} 条结论作答，引用证据 {'、'.join(evidence_ids)}。"
            )

        scope = source_ids or [
            item["source_id"] for item in sources["sources"] if item["already_indexed"]
        ]
        if not scope:
            return self._say("当前项目没有可用于回答的已索引文献。")
        return self._call(
            "literature_answer",
            {"question": self._corpus.question, "source_ids": scope},
        )


ANSWER_PROMPT_MARKER = "Answer the research question using only the evidence listed below"
EVIDENCE_ID_RE = __import__("re").compile(r"\[(evidence_[0-9a-f]{24})\]")


class RoleRouter:
    """One injected provider serving both the planner and the answer model.

    The application builds its answer model through the same Agent provider
    factory, so the two roles share one object here and are told apart by the
    shape of the request: the answer model is called with no tools and the frozen
    evidence prompt.
    """

    provider_name = "scripted"
    model = "scripted-a1-v1"
    base_url = ""

    def __init__(self) -> None:
        self.planner: LibraryAgentProvider | None = None

    @staticmethod
    def _is_answer_call(prompt: str, tools) -> bool:
        return not tools and ANSWER_PROMPT_MARKER in prompt

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        prompt = messages[0].text_content() if messages else ""
        if self._is_answer_call(prompt, tools):
            evidence_ids = EVIDENCE_ID_RE.findall(prompt)
            return ProviderResponse(
                blocks=[
                    TextBlock(
                        text=json.dumps(
                            {
                                "claims": [
                                    {
                                        "text": "The selected demo papers report this protocol.",
                                        "evidence_ids": evidence_ids,
                                        "evidence_status": "supported",
                                    }
                                ]
                            }
                        )
                    )
                ]
            )
        if self.planner is None:
            raise AssertionError("no planner script was installed for this turn")
        return await self.planner.chat(
            messages, tools, system_prompt, max_tokens, temperature, tool_choice
        )


class ForeignSourceProvider(LibraryAgentProvider):
    """Plans the chain but asks the answer tool for a source outside the project."""

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        by_name = self._results(messages)
        if "literature_answer" not in by_name:
            return self._call(
                "literature_answer",
                {"question": "Q?", "source_ids": ["src_other_project0001"]},
            )
        answer = by_name["literature_answer"][-1]
        if answer.is_error:
            return self._say(f"工具被拒绝：{answer.output}")
        return self._say("不应该走到这里")


async def run_turn(
    runtime: ConversationRuntime, message: str, *, decisions: dict[str, str] | None = None
) -> list:
    """Run one turn and answer every approval with the scripted decision."""

    events = []
    async for event in runtime.turn(message):
        events.append(event)
        if event.type is AgentEventType.AWAIT_APPROVAL:
            tool_name = str(event.data.get("tool_name", ""))
            decision = (decisions or {}).get(tool_name, "allow_once")
            runtime.approve(str(event.data.get("id", "")), decision)
    return events


def tool_result_for(events: list, tool_name: str):
    for event in events:
        if event.type is not AgentEventType.TOOL_RESULT:
            continue
        if (
            event.data.get("tool_name") == tool_name
            or event.data.get("metadata", {}).get("tool_name") == tool_name
        ):
            return event
    return None


@pytest.fixture(scope="module")
def client_and_patches():
    pytest.importorskip("chromadb")
    from api_factory import create_app

    helpers = importlib.import_module("tests.integration.test_literature_demo_e2e")
    corpus = helpers.load_demo_corpus()
    test_dir = Path(tempfile.mkdtemp(prefix="a1-session-"))
    config_dir = test_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "default.yaml").write_text(CONFIG, encoding="utf-8")

    stub = helpers.OfflineArxivStub(corpus, open_fulltext=True)
    downloader = helpers.CorpusDownloader(corpus, test_dir / "downloads")
    roles = RoleRouter()

    with (
        patch("api_factory.CONFIG_PATH", config_dir / "default.yaml"),
        patch("api_factory.RUNTIME_DIR", test_dir),
        patch("api_factory.BASE_DIR", test_dir),
        patch("src.literature.providers.arxiv.ArxivProvider", lambda *a, **k: stub),
        patch("src.literature.fulltext.HttpFullTextDownloader", lambda *a, **k: downloader),
        # Same seam as D-024: the model is substituted before the app is created.
        patch("src.agent_v2.router._create_provider", lambda *a, **k: roles),
    ):
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client, app, corpus, helpers, test_dir, roles

    shutil.rmtree(test_dir, ignore_errors=True)


def asgi_transport(app):
    return httpx.ASGITransport(app=app)


def patch_tool_http(monkeypatch: pytest.MonkeyPatch, app) -> None:
    """Serve the tools' ``SCHOLAR_API_BASE`` calls from the app itself."""

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        forwarded = {
            key: value for key, value in kwargs.items() if key in {"timeout", "follow_redirects"}
        }
        return real_client(
            transport=asgi_transport(app),
            base_url="http://localhost:18088",
            **forwarded,
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def tool_names(events: list, event_type: AgentEventType) -> list[str]:
    return [str(event.data.get("tool_name")) for event in events if event.type is event_type]


def output_for(events: list, tool_name: str) -> str:
    return next(
        event.data["output"]
        for event in events
        if event.type is AgentEventType.TOOL_RESULT and event.data.get("tool_name") == tool_name
    )


def run_turn_sync(
    runtime: ConversationRuntime, message: str, *, decisions: dict[str, str] | None = None
) -> list:
    import asyncio

    return asyncio.run(run_turn(runtime, message, decisions=decisions))


def build_runtime(
    project: str, planner: LibraryAgentProvider, roles: RoleRouter
) -> ConversationRuntime:
    import src.agent_v2.router as router

    roles.planner = planner
    runtime = router._create_runtime(project)
    assert runtime is not None
    return runtime


def create_project(client: TestClient, workdir: Path, name: str) -> str:
    location = workdir / "projects"
    location.mkdir(parents=True, exist_ok=True)
    created = client.post(
        "/api/project/create",
        json={
            "name": name,
            "location": str(location),
            "template_id": "research_paper",
            "init_git": False,
        },
    )
    assert created.status_code == 200, created.text
    return str(created.json()["project_path"])


def test_agent_runs_the_whole_literature_chain_with_confirmations(
    client_and_patches, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, corpus, helpers, test_dir, roles = client_and_patches
    project = create_project(client, test_dir / "chain", "A1 Chain")
    provider = LibraryAgentProvider(corpus, helpers)
    runtime = build_runtime(project, provider, roles)
    patch_tool_http(monkeypatch, app)

    events = run_turn_sync(runtime, "请用我项目里的文献回答我的研究问题")

    approvals = [e for e in events if e.type is AgentEventType.AWAIT_APPROVAL]
    approved_tools = [str(e.data.get("tool_name")) for e in approvals]
    imported = json.loads(output_for(events, "literature_import"))
    source_count = len(imported["sources"])
    assert source_count >= 2
    # Every side-effecting literature step stopped for confirmation, in order.
    assert approved_tools == [
        "literature_search",
        "literature_import",
        *["literature_acquire_fulltext"] * source_count,
        *["literature_index"] * source_count,
        "literature_answer",
    ]

    calls = tool_names(events, AgentEventType.TOOL_CALL)
    assert calls[0] == "literature_sources"

    sources = json.loads(output_for(events, "literature_sources"))
    assert sources["source_count"] == 0

    answer = json.loads(output_for(events, "literature_answer"))
    assert answer["status"] == "answered"
    assert answer["claims"] and answer["evidence"]
    assert answer["source_ids"]
    for item in answer["evidence"]:
        assert item["page"] >= 1
        assert item["exact_quote"]

    final = [e for e in events if e.type is AgentEventType.RESPONSE]
    assert final and "引用证据" in final[-1].data.get("text", "")


def test_denied_confirmation_stops_the_chain_without_calling_the_service(
    client_and_patches, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, corpus, helpers, test_dir, roles = client_and_patches
    project = create_project(client, test_dir / "deny", "A1 Deny")
    provider = LibraryAgentProvider(corpus, helpers)
    runtime = build_runtime(project, provider, roles)
    patch_tool_http(monkeypatch, app)

    events = run_turn_sync(runtime, "检索新文献", decisions={"literature_search": "deny"})

    types = [e.type for e in events]
    assert AgentEventType.APPROVAL_RECEIVED in types
    denial = output_for(events, "literature_search")
    assert "denied" in json.dumps(denial, ensure_ascii=False).lower()
    # No search ever reached the service: no execution handle was produced.
    assert "search_exec_" not in denial


def test_out_of_scope_source_is_refused_by_the_answer_tool(
    client_and_patches, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, corpus, helpers, test_dir, roles = client_and_patches
    project = create_project(client, test_dir / "foreign", "A1 Foreign")
    provider = ForeignSourceProvider(corpus, helpers)
    runtime = build_runtime(project, provider, roles)
    patch_tool_http(monkeypatch, app)

    events = run_turn_sync(runtime, "回答我")

    answer_event = next(
        e
        for e in events
        if e.type is AgentEventType.TOOL_RESULT and e.data.get("tool_name") == "literature_answer"
    )
    assert answer_event.data.get("is_error") is True
    assert "source_not_found" in answer_event.data["output"]


def test_second_turn_reuses_existing_index_instead_of_reindexing(
    client_and_patches, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, corpus, helpers, test_dir, roles = client_and_patches
    project = create_project(client, test_dir / "reuse", "A1 Reuse")
    patch_tool_http(monkeypatch, app)

    first = LibraryAgentProvider(corpus, helpers)
    first_events = run_turn_sync(build_runtime(project, first, roles), "建库并回答")
    assert "literature_index" in tool_names(first_events, AgentEventType.TOOL_RESULT)

    second = LibraryAgentProvider(corpus, helpers, mode="reuse_existing")
    second_events = run_turn_sync(build_runtime(project, second, roles), "再回答一次")

    second_calls = tool_names(second_events, AgentEventType.TOOL_CALL)
    assert "literature_index" not in second_calls
    assert "literature_acquire_fulltext" not in second_calls
    assert second_calls[-1] == "literature_answer"

    sources = json.loads(output_for(second_events, "literature_sources"))
    assert sources["indexed_count"] >= 1
    assert all(
        item["already_indexed"] for item in sources["sources"] if item["rag_status"] == "ready"
    )
