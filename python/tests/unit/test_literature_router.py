"""HTTP contract tests for literature search and project import."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.literature import ProjectSourceManifestStore, register_literature_routes
from routers.project import ProjectSourceUpsert, _upsert_source, register_project
from src.literature.models import ExternalIdentifiers, PaperRecord, SearchQuery
from src.literature.providers.base import (
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)
from src.literature.providers.fixture import FixtureProvider
from src.literature.service import LiteratureService

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


def make_app(tmp_path: Path, provider: FixtureProvider) -> FastAPI:
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
