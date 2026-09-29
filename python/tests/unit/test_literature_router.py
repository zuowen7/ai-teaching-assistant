"""HTTP contract tests for literature search and project import."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.literature import ProjectSourceManifestStore, register_literature_routes
from routers.project import ProjectSourceUpsert, _upsert_source, register_project
from src.literature.answer_model import build_model_identity
from src.literature.evidence import sha256_file
from src.literature.models import ExternalIdentifiers, PaperRecord, SearchQuery
from src.literature.providers.base import (
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)
from src.literature.providers.fixture import FixtureProvider
from src.literature.service import LiteratureService, RetrievedChunk
from tests.unit.test_literature_indexing import (
    PAGE_ONE,
    PAGE_TWO,
    MemoryPageIndexStore,
    write_pdf,
)

NOW = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
QUERY = 'all:"multi agent writing"'
RESEARCH_QUESTION = "How are multi-agent systems used in academic writing?"


def search_plan_payload() -> dict:
    return {
        "research_question": RESEARCH_QUESTION,
        "suggested_query": 'all:"multi agent academic writing"',
        "generation_method": "template",
        "generation_model": None,
        "generation_config": {"template": "all_phrase_v1"},
    }


def search_request_payload(*, provider: str = "fixture") -> dict:
    return {
        "provider": provider,
        "query": SearchQuery(query=QUERY).model_dump(mode="json"),
        "plan": search_plan_payload(),
    }


def make_record() -> PaperRecord:
    return PaperRecord(
        provider="fixture",
        provider_record_id="cs/9901001",
        external_ids=ExternalIdentifiers(
            doi="10.1000/router-test",
            arxiv="cs/9901001v2",
        ),
        title="Multi-Agent Writing Assistance",
        authors=["Ada Researcher", "Lin Reviewer"],
        year=2026,
        venue="PoC Proceedings",
        abstract="A deterministic record for route verification.",
        categories=["cs.AI"],
        record_url="https://arxiv.org/abs/cs/9901001v2",
        access_locations=[],
        source_query=QUERY,
        retrieved_at=NOW,
    )


def make_provider(
    *,
    records: list[PaperRecord] | None = None,
    error: LiteratureProviderError | None = None,
) -> FixtureProvider:
    configured = records if records is not None else [make_record()]
    return FixtureProvider(
        fixture_name="router-fixture-v1",
        snapshot_at=NOW,
        records=configured,
        search_results={QUERY: [record.paper_id for record in configured]},
        operation_errors={ProviderOperation.SEARCH: error} if error else None,
    )


def make_app(
    tmp_path: Path,
    provider: FixtureProvider,
    *,
    index_store=None,
    retriever=None,
    answer_model=None,
) -> FastAPI:
    app = FastAPI()
    register_project(
        app,
        cloud_only=False,
        load_config=lambda: {"translator": {}, "agent": {}},
        runtime_dir=tmp_path,
        data_root=tmp_path / "data",
    )
    register_literature_routes(
        app,
        service=LiteratureService(
            providers=[provider],
            project_store=ProjectSourceManifestStore(),
            index_store=index_store,
            retriever=retriever,
            answer_model=answer_model,
        ),
    )
    return app


def create_project(client: TestClient, tmp_path: Path) -> Path:
    location = tmp_path / "projects"
    location.mkdir(exist_ok=True)
    response = client.post(
        "/api/project/create",
        json={
            "name": "LiteraturePoC",
            "location": str(location),
            "template_id": "research_paper",
            "init_git": False,
        },
    )
    assert response.status_code == 200
    return Path(response.json()["project_path"])


def execute_search(client: TestClient) -> dict:
    response = client.post(
        "/api/literature/search",
        json=search_request_payload(),
    )
    assert response.status_code == 200
    return response.json()


def test_search_select_import_and_repeat_reuse_existing_project_source(tmp_path: Path) -> None:
    record = make_record()
    with TestClient(make_app(tmp_path, make_provider(records=[record]))) as client:
        project = create_project(client, tmp_path)
        providers = client.get("/api/literature/providers")
        assert providers.status_code == 200
        assert providers.json()["providers"][0]["provider"] == "fixture"

        execution = execute_search(client)
        page = execution["page"]
        assert execution["search_execution_id"].startswith("search_exec_")
        assert len(execution["search_execution_id"]) == len("search_exec_") + 32
        assert page["result_mode"] == "fixture"
        assert page["provenance_label"] == "router-fixture-v1"
        assert page["records"][0]["paper_id"] == record.paper_id
        assert execution["plan"]["research_question"] == RESEARCH_QUESTION
        assert execution["plan"]["executed_query"]["query"] == QUERY
        assert execution["plan"]["result_snapshot_id"] == page["result_snapshot_id"]

        payload = {
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record.paper_id],
        }
        imported = client.post("/api/literature/import", json=payload)
        assert imported.status_code == 200
        assert imported.json()["created_count"] == 1
        assert imported.json()["search_execution_id"] == execution["search_execution_id"]
        assert imported.json()["result_snapshot_id"] == page["result_snapshot_id"]
        source_id = imported.json()["results"][0]["source_id"]
        assert source_id.startswith("src_lit_")
        assert source_id != record.paper_id

        repeated = client.post("/api/literature/import", json=payload)
        assert repeated.status_code == 200
        assert repeated.json()["reused_count"] == 1
        assert repeated.json()["results"][0]["source_id"] == source_id

        listed = client.get("/api/project/sources", params={"project_path": str(project)})
        assert listed.status_code == 200
        assert len(listed.json()["sources"]) == 1
        source = listed.json()["sources"][0]
        assert source["original_path"] is None
        assert source["rag_status"] == "unavailable"
        assert source["metadata"]["literature"]["fulltext"]["status"] == "metadata_only"
        event = source["metadata"]["literature"]["search_events"][0]
        plan = event["search_plan"]
        assert plan["research_question"] == RESEARCH_QUESTION
        assert plan["suggested_query"] == 'all:"multi agent academic writing"'
        assert plan["generation_method"] == "template"
        assert plan["generation_model"] is None
        assert plan["generation_config"] == {"template": "all_phrase_v1"}
        assert plan["provider"] == "fixture"
        assert plan["executed_query"]["query"] == QUERY
        assert plan["result_snapshot_id"] == page["result_snapshot_id"]
        assert event["search_execution_id"] == execution["search_execution_id"]
        assert plan == execution["plan"]
        confirmed_at = datetime.fromisoformat(plan["confirmed_at"])
        executed_at = datetime.fromisoformat(plan["executed_at"])
        assert confirmed_at.tzinfo is not None
        assert executed_at.tzinfo is not None
        assert executed_at >= confirmed_at


def test_index_and_resolve_evidence_over_http(tmp_path: Path) -> None:
    """The two P2B routes compose the real project store with a page index."""

    record = make_record()
    index_store = MemoryPageIndexStore()
    with TestClient(
        make_app(tmp_path, make_provider(records=[record]), index_store=index_store)
    ) as client:
        project = create_project(client, tmp_path)
        execution = execute_search(client)
        imported = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [record.paper_id],
            },
        )
        assert imported.status_code == 200
        source_id = imported.json()["results"][0]["source_id"]

        pdf = write_pdf(tmp_path / "attached.pdf", [PAGE_ONE, PAGE_TWO])

        missing = client.post(
            "/api/literature/index",
            json={"project_path": str(project), "source_id": source_id},
        )
        assert missing.status_code == 409
        assert missing.json()["detail"]["code"] == "source_artifact_missing"

        with pdf.open("rb") as stream:
            attached = client.post(
                "/api/project/sources/import",
                data={"project_path": str(project), "source_id": source_id},
                files={"file": (pdf.name, stream, "application/pdf")},
            )
        assert attached.status_code == 200
        assert attached.json()["id"] == source_id
        assert attached.json()["metadata"]["literature"]["fulltext"]["status"] == "metadata_only"

        indexed = client.post(
            "/api/literature/index",
            json={"project_path": str(project), "source_id": source_id},
        )
        assert indexed.status_code == 200
        body = indexed.json()
        assert body["status"] == "indexed"
        assert body["chunk_count"] == len(index_store.chunks) >= 2
        assert body["page_count"] == 2
        assert body["artifact_sha256"] == sha256_file(pdf)
        assert body["reused"] is False

        chunk_id = index_store.chunk_ids_on_page(2)[0]
        resolved = client.post(
            "/api/literature/evidence",
            json={"project_path": str(project), "source_id": source_id, "chunk_id": chunk_id},
        )
        assert resolved.status_code == 200
        span = resolved.json()["span"]
        assert span["page_start"] == span["page_end"] == 2
        assert span["artifact_sha256"] == body["artifact_sha256"]
        assert span["coordinate_space"] == "normalized_page_text_v1"
        assert span["exact_quote"]
        assert resolved.json()["paper_id"] == record.paper_id

        unknown = client.post(
            "/api/literature/evidence",
            json={
                "project_path": str(project),
                "source_id": source_id,
                "chunk_id": "chunk_missing",
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "chunk_not_found"

        sources = client.get("/api/project/sources", params={"project_path": str(project)}).json()
        literature = sources["sources"][0]["metadata"]["literature"]
        assert literature["fulltext"]["status"] == "indexed"
        assert literature["index"]["chunk_count"] == body["chunk_count"]
        assert literature["index"]["artifact_sha256"] == body["artifact_sha256"]


def test_fulltext_route_reports_missing_wiring_and_unknown_sources(tmp_path: Path) -> None:
    """HTTP mapping for M5 acquisition: unknown source is 404, unwired downloader 503."""

    record = make_record()
    with TestClient(make_app(tmp_path, make_provider(records=[record]))) as client:
        project = create_project(client, tmp_path)
        execution = execute_search(client)
        imported = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [record.paper_id],
            },
        )
        assert imported.status_code == 200
        source_id = imported.json()["results"][0]["source_id"]

        unknown = client.post(
            "/api/literature/fulltext",
            json={"project_path": str(project), "source_id": "src_lit_unknown"},
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "source_not_found"

        unwired = client.post(
            "/api/literature/fulltext",
            json={"project_path": str(project), "source_id": source_id},
        )
        assert unwired.status_code == 503
        assert unwired.json()["detail"]["code"] == "downloader_unavailable"

        rejected = client.post(
            "/api/literature/fulltext",
            json={
                "project_path": str(project),
                "source_id": source_id,
                "local_path": "D:/tmp/evil.pdf",
            },
        )
        assert rejected.status_code == 422


def test_regular_source_update_cannot_delete_or_replace_literature_metadata(
    tmp_path: Path,
) -> None:
    record = make_record()
    with TestClient(make_app(tmp_path, make_provider(records=[record]))) as client:
        project = create_project(client, tmp_path)
        execution = execute_search(client)
        selection = {
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record.paper_id],
        }
        created = client.post("/api/literature/import", json=selection).json()["results"][0][
            "source"
        ]

        updated = client.post(
            "/api/project/sources",
            json={
                "project_path": str(project),
                "source_id": created["id"],
                "title": created["title"],
                "original_path": created["original_path"],
                "translated_path": created["translated_path"],
                "translation_task_id": created["translation_task_id"],
                "rag_status": created["rag_status"],
                "reading_status": "read",
                "cited": True,
                "metadata": {},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["metadata"]["literature"]["primary_paper_id"] == record.paper_id

        tampered = client.post(
            "/api/project/sources",
            json={
                "project_path": str(project),
                "source_id": created["id"],
                "title": created["title"],
                "metadata": {"literature": {"schema_version": 999}},
            },
        )
        assert tampered.status_code == 409

        repeated = client.post("/api/literature/import", json=selection)
        assert repeated.status_code == 200
        assert repeated.json()["reused_count"] == 1
        listed = client.get(
            "/api/project/sources",
            params={"project_path": str(project)},
        ).json()["sources"]
        assert len(listed) == 1
        assert listed[0]["metadata"]["literature"]["primary_paper_id"] == record.paper_id


def test_literature_transaction_and_regular_upsert_cannot_lose_each_other(
    tmp_path: Path,
) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        project = create_project(client, tmp_path)

    entered = Event()
    release = Event()
    regular_started = Event()
    regular_done = Event()
    failures: list[BaseException] = []
    store = ProjectSourceManifestStore()

    def literature_update(sources: list[dict]) -> list[dict]:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test transaction was not released")
        return [
            {
                "id": "src_lit_transaction",
                "title": "Literature transaction",
                "metadata": {},
            },
            *sources,
        ]

    def run_literature_update() -> None:
        try:
            store.update_sources(str(project), literature_update)
        except BaseException as exc:
            failures.append(exc)

    def run_regular_upsert() -> None:
        try:
            regular_started.set()
            _upsert_source(
                ProjectSourceUpsert(
                    project_path=str(project),
                    source_id="src_regular_upsert",
                    title="Regular upsert",
                )
            )
            regular_done.set()
        except BaseException as exc:
            failures.append(exc)

    literature_thread = Thread(target=run_literature_update)
    regular_thread = Thread(target=run_regular_upsert)
    literature_thread.start()
    assert entered.wait(2)
    regular_thread.start()
    assert regular_started.wait(2)
    assert not regular_done.wait(0.1)
    release.set()
    literature_thread.join(5)
    regular_thread.join(5)

    assert not literature_thread.is_alive()
    assert not regular_thread.is_alive()
    assert failures == []
    with TestClient(make_app(tmp_path, make_provider())) as client:
        listed = client.get("/api/project/sources", params={"project_path": str(project)})
    assert listed.status_code == 200
    assert {source["id"] for source in listed.json()["sources"]} == {
        "src_lit_transaction",
        "src_regular_upsert",
    }


def test_get_record_uses_query_parameter_for_legacy_arxiv_identifier(tmp_path: Path) -> None:
    record = make_record()
    with TestClient(make_app(tmp_path, make_provider(records=[record]))) as client:
        response = client.get(
            "/api/literature/record",
            params={"provider": "fixture", "external_id": "cs/9901001v2"},
        )

    assert response.status_code == 200
    assert response.json()["paper_id"] == record.paper_id


def test_empty_fixture_result_is_a_successful_page(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, make_provider(records=[]))) as client:
        response = client.post(
            "/api/literature/search",
            json=search_request_payload(),
        )

    assert response.status_code == 200
    execution = response.json()
    assert execution["page"]["records"] == []
    assert execution["page"]["total_results"] == 0
    assert execution["plan"]["result_snapshot_id"] == execution["page"]["result_snapshot_id"]


@pytest.mark.parametrize(
    "extra_field",
    ["source_id", "local_path", "fulltext_status", "result_snapshot_id"],
)
def test_import_rejects_caller_controlled_identity_path_and_state(
    tmp_path: Path,
    extra_field: str,
) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        project = create_project(client, tmp_path)
        execution = execute_search(client)
        payload = {
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            extra_field: "caller-controlled",
        }
        response = client.post("/api/literature/import", json=payload)

    assert response.status_code == 422
    assert not (project / ".yanmo" / "sources.json").exists()


@pytest.mark.parametrize(
    ("project_path", "expected_status"),
    [
        ("relative/project", 422),
        ("C:/tmp/../escape", 422),
        ("bad\x00path", 422),
    ],
)
def test_import_reuses_project_path_validation(
    tmp_path: Path,
    project_path: str,
    expected_status: int,
) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        execution = execute_search(client)
        response = client.post(
            "/api/literature/import",
            json={
                "project_path": project_path,
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            },
        )

    assert response.status_code == expected_status


def test_import_rejects_allowed_but_non_project_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with TestClient(make_app(tmp_path, make_provider())) as client:
        execution = execute_search(client)
        response = client.post(
            "/api/literature/import",
            json={
                "project_path": str(plain),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            },
        )

    assert response.status_code == 404
    assert not (plain / ".yanmo" / "sources.json").exists()


def test_import_refuses_to_overwrite_structurally_invalid_manifest(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        project = create_project(client, tmp_path)
        manifest = project / ".yanmo" / "sources.json"
        original = '{"version": 1, "sources": {"not": "a list"}}'
        manifest.write_text(original, encoding="utf-8")
        execution = execute_search(client)
        response = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            },
        )

    assert response.status_code == 500
    assert "结构无效" in response.json()["detail"]
    assert manifest.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("version_json", ["2", "true", "1.0"])
def test_import_refuses_unknown_manifest_version_without_rewriting(
    tmp_path: Path,
    version_json: str,
) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        project = create_project(client, tmp_path)
        manifest = project / ".yanmo" / "sources.json"
        original = f'{{"version": {version_json}, "sources": []}}'
        manifest.write_text(original, encoding="utf-8")
        execution = execute_search(client)
        response = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            },
        )

    assert response.status_code == 409
    assert manifest.read_text(encoding="utf-8") == original


def test_project_metadata_link_cannot_escape_project_boundary(tmp_path: Path) -> None:
    project = tmp_path / "linked-project"
    outside = tmp_path / "outside-metadata"
    project.mkdir()
    outside.mkdir()
    (outside / "project.json").write_text("{}", encoding="utf-8")
    try:
        (project / ".yanmo").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlink unavailable: {exc}")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(project / ".yanmo"), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"directory link unavailable: {junction.stderr or junction.stdout}")

    with TestClient(make_app(tmp_path, make_provider())) as client:
        execution = execute_search(client)
        response = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [execution["page"]["records"][0]["paper_id"]],
            },
        )

    assert response.status_code == 403
    assert not (outside / "sources.json").exists()


def test_unknown_provider_and_missing_execution_are_explicit_failures(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        unknown = client.post(
            "/api/literature/search",
            json=search_request_payload(provider="missing"),
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "unknown_provider"

        project = create_project(client, tmp_path)
        missing_execution = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": f"search_exec_{'0' * 32}",
                "paper_ids": [make_record().paper_id],
            },
        )

    assert missing_execution.status_code == 404
    assert missing_execution.json()["detail"]["code"] == "search_execution_not_found"


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (ProviderErrorCode.INVALID_REQUEST, 400),
        (ProviderErrorCode.NOT_FOUND, 404),
        (ProviderErrorCode.RATE_LIMITED, 429),
        (ProviderErrorCode.UNAVAILABLE, 503),
        (ProviderErrorCode.INVALID_RESPONSE, 502),
    ],
)
def test_provider_errors_are_mapped_without_upstream_details(
    tmp_path: Path,
    code: ProviderErrorCode,
    expected_status: int,
) -> None:
    error = LiteratureProviderError(
        code,
        "SECRET upstream response body",
        provider="fixture",
        operation=ProviderOperation.SEARCH,
        retry_after_seconds=3.2 if code is ProviderErrorCode.RATE_LIMITED else None,
        details={"response_excerpt": "TOP SECRET"},
    )
    with TestClient(make_app(tmp_path, make_provider(error=error))) as client:
        response = client.post(
            "/api/literature/search",
            json=search_request_payload(),
        )

    assert response.status_code == expected_status
    body = response.json()
    assert body["detail"]["code"] == code.value
    assert "SECRET" not in response.text
    assert "details" not in body["detail"]
    if code is ProviderErrorCode.RATE_LIMITED:
        assert response.headers["retry-after"] == "4"


def test_search_request_forbids_uncontracted_fields(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        payload = search_request_payload()
        payload["raw_xml"] = True
        response = client.post(
            "/api/literature/search",
            json=payload,
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "fixture", "query": {"query": QUERY}},
        {
            "provider": "fixture",
            "query": {"query": QUERY},
            "plan": {
                "research_question": RESEARCH_QUESTION,
                "suggested_query": QUERY,
                "generation_method": "llm",
                "generation_model": None,
                "generation_config": {},
            },
        },
    ],
    ids=["missing-plan", "llm-plan-without-model"],
)
def test_search_request_requires_valid_plan(tmp_path: Path, payload: dict) -> None:
    with TestClient(make_app(tmp_path, make_provider())) as client:
        response = client.post("/api/literature/search", json=payload)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# P3 evidence answer route (plan 5.9)
# ---------------------------------------------------------------------------

EVIDENCE_ID_RE = re.compile(r"\[(evidence_[0-9a-f]{24})\]")


class RouterRetriever:
    """Scoped retriever whose hits the test controls directly."""

    def __init__(self) -> None:
        self.chunk_ids: list[str] = []
        self.source_id = ""
        self.calls: list[dict] = []

    async def retrieve(self, *, project_root, source_ids, query, top_k):
        self.calls.append(
            {
                "project_root": project_root,
                "source_ids": list(source_ids),
                "query": query,
                "top_k": top_k,
            }
        )
        return [
            RetrievedChunk(chunk_id=chunk_id, source_id=self.source_id)
            for chunk_id in self.chunk_ids
        ]


class RouterAnswerModel:
    """Answer model that cites the first evidence id it is offered."""

    def __init__(self, *, response: str | None = None) -> None:
        self.identity = build_model_identity(provider="openai", model="gpt-4o")
        self.response = response
        self.prompts: list[str] = []

    async def complete(self, *, system_prompt: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.response is not None:
            return self.response
        evidence_ids = EVIDENCE_ID_RE.findall(prompt)
        return json.dumps(
            {
                "claims": [
                    {
                        "text": "The selected paper reports this result.",
                        "evidence_ids": evidence_ids[:1],
                        "evidence_status": "supported",
                    }
                ]
            }
        )


def prepare_indexed_project(client: TestClient, tmp_path: Path, index_store) -> tuple[str, Path]:
    """Search, import, attach a real PDF and index it; return the source id."""

    record = make_record()
    project = create_project(client, tmp_path)
    execution = execute_search(client)
    imported = client.post(
        "/api/literature/import",
        json={
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record.paper_id],
        },
    )
    assert imported.status_code == 200
    source_id = imported.json()["results"][0]["source_id"]

    pdf = write_pdf(tmp_path / "answer.pdf", [PAGE_ONE, PAGE_TWO])
    with pdf.open("rb") as stream:
        attached = client.post(
            "/api/project/sources/import",
            data={"project_path": str(project), "source_id": source_id},
            files={"file": (pdf.name, stream, "application/pdf")},
        )
    assert attached.status_code == 200
    indexed = client.post(
        "/api/literature/index",
        json={"project_path": str(project), "source_id": source_id},
    )
    assert indexed.status_code == 200
    return source_id, project


def answer_request(project: Path, source_ids: list[str], **overrides) -> dict:
    payload = {
        "project_path": str(project),
        "question": "Does the reported result hold?",
        "source_ids": source_ids,
        "top_k": 5,
    }
    payload.update(overrides)
    return payload


def test_answer_route_returns_machine_checked_evidence(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    retriever = RouterRetriever()
    model = RouterAnswerModel()
    app = make_app(
        tmp_path,
        make_provider(),
        index_store=index_store,
        retriever=retriever,
        answer_model=model,
    )
    with TestClient(app) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)
        retriever.source_id = source_id
        retriever.chunk_ids = index_store.chunk_ids_on_page(1)

        answered = client.post("/api/literature/answer", json=answer_request(project, [source_id]))

        assert answered.status_code == 200
        body = answered.json()
        assert body["status"] == "answered"
        assert body["insufficient_reason"] is None
        assert body["source_ids"] == [source_id]
        assert body["retrieved_chunk_count"] == len(retriever.chunk_ids)
        assert body["model_provider"] == model.identity.provider
        assert body["model_name"] == model.identity.model
        assert body["model_config_hash"] == model.identity.config_hash

        assert len(body["claims"]) == 1
        claim = body["claims"][0]
        assert claim["claim_id"].startswith("claim_")
        assert claim["evidence_status"] == "supported"

        assert len(body["evidence"]) == 1
        evidence = body["evidence"][0]
        assert claim["evidence_ids"] == [evidence["span"]["evidence_id"]]
        assert evidence["source_id"] == source_id
        assert evidence["span"]["page_start"] == evidence["span"]["page_end"] == 1
        assert evidence["span"]["exact_quote"]
        assert evidence["span"]["coordinate_space"] == "normalized_page_text_v1"
        assert retriever.calls[0]["project_root"] == str(project)
        assert retriever.calls[0]["source_ids"] == [source_id]


def test_answer_route_requires_an_explicit_source_scope(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    retriever = RouterRetriever()
    app = make_app(
        tmp_path,
        make_provider(),
        index_store=index_store,
        retriever=retriever,
        answer_model=RouterAnswerModel(),
    )
    with TestClient(app) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)

        no_scope = client.post("/api/literature/answer", json=answer_request(project, []))
        assert no_scope.status_code == 400
        assert no_scope.json()["detail"]["code"] == "scope_required"

        unknown = client.post(
            "/api/literature/answer",
            json=answer_request(project, ["src_missing000000000000"]),
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "source_not_found"

        bad_top_k = client.post(
            "/api/literature/answer", json=answer_request(project, [source_id], top_k=99)
        )
        assert bad_top_k.status_code == 422

        assert retriever.calls == []


def test_answer_route_forbids_client_supplied_evidence(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    app = make_app(
        tmp_path,
        make_provider(),
        index_store=index_store,
        retriever=RouterRetriever(),
        answer_model=RouterAnswerModel(),
    )
    with TestClient(app) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)

        for extra in (
            {"chunk_id": "chunk_forged"},
            {"evidence": [{"evidence_id": "evidence_" + "f" * 24}]},
            {"answer": "forged"},
        ):
            response = client.post(
                "/api/literature/answer",
                json=answer_request(project, [source_id], **extra),
            )
            assert response.status_code == 422


def test_answer_route_reports_insufficient_without_calling_the_model(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    retriever = RouterRetriever()
    model = RouterAnswerModel()
    app = make_app(
        tmp_path,
        make_provider(),
        index_store=index_store,
        retriever=retriever,
        answer_model=model,
    )
    with TestClient(app) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)
        retriever.source_id = source_id
        retriever.chunk_ids = []

        empty = client.post("/api/literature/answer", json=answer_request(project, [source_id]))
        assert empty.status_code == 200
        body = empty.json()
        assert body["status"] == "insufficient"
        assert body["insufficient_reason"] == "no_retrieval_hits"
        assert body["claims"] == []
        assert body["evidence"] == []
        assert model.prompts == []

        retriever.chunk_ids = ["chunk_that_vanished"]
        unresolved = client.post(
            "/api/literature/answer", json=answer_request(project, [source_id])
        )
        assert unresolved.status_code == 200
        assert unresolved.json()["insufficient_reason"] == "no_resolvable_evidence"
        assert unresolved.json()["unresolved"][0]["reason"] == "chunk_not_found"
        assert model.prompts == []


def test_answer_route_maps_failure_codes(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    retriever = RouterRetriever()
    model = RouterAnswerModel()
    app = make_app(
        tmp_path,
        make_provider(),
        index_store=index_store,
        retriever=retriever,
        answer_model=model,
    )
    with TestClient(app) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)
        retriever.source_id = source_id
        retriever.chunk_ids = index_store.chunk_ids_on_page(1)

        model.response = "not json at all"
        invalid = client.post("/api/literature/answer", json=answer_request(project, [source_id]))
        assert invalid.status_code == 502
        assert invalid.json()["detail"]["code"] == "answer_invalid_response"


def test_answer_route_reports_missing_wiring(tmp_path: Path) -> None:
    index_store = MemoryPageIndexStore()
    with TestClient(make_app(tmp_path, make_provider(), index_store=index_store)) as client:
        source_id, project = prepare_indexed_project(client, tmp_path, index_store)

        unwired = client.post("/api/literature/answer", json=answer_request(project, [source_id]))
        assert unwired.status_code == 503
        assert unwired.json()["detail"]["code"] == "retrieval_unavailable"


@pytest.mark.parametrize(
    "extra",
    [
        {"exact_quote": "a quote the caller invented"},
        {"evidence_id": "evidence_" + "f" * 24},
        {"char_start": 0, "char_end": 5},
        {"page_start": 1, "page_end": 2},
    ],
    ids=["quote", "evidence-id", "char-coordinates", "page-coordinates"],
)
def test_evidence_route_refuses_client_supplied_quote_and_coordinates(
    tmp_path: Path, extra: dict
) -> None:
    """The caller may only name a chunk; quotes and coordinates are derived."""

    with TestClient(make_app(tmp_path, make_provider())) as client:
        response = client.post(
            "/api/literature/evidence",
            json={
                "project_path": str(tmp_path),
                "source_id": "src_alpha",
                "chunk_id": "chunk_alpha",
                **extra,
            },
        )

    assert response.status_code == 422
