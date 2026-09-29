"""Fixed P4 demo script (plan 5.10, decisions D-034/D-035).

The script walks the documented demo order against the *real* HTTP API:

    search (confirmed query) -> import -> acquire open full text
      -> attach a local full text when acquisition is unavailable
      -> index -> scoped evidence answer -> resolve one evidence span

Every step is recorded with a status, a reason when it does not succeed, its
duration and its counts, and the finished run is written as one JSON file under
``methods/literature_poc/runs/``.  The caller keeps the two confirmation points
(the query and the selection) — there is deliberately no server-side "one click
demo" endpoint (D-034).

The same function drives both modes: ``--provider fixture`` uses the offline
corpus shipped in ``config/literature_demo_corpus.json``; ``--provider arxiv``
uses the live provider and only skips the local attachment step when a paper has
no open full text.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.literature.demo_corpus import (  # noqa: E402
    DemoCorpus,
    DemoCorpusError,
    load_demo_corpus,
    materialize_pdf,
)
from src.literature.demo_run import (  # noqa: E402
    DemoRun,
    DemoRunRecorder,
    StepStatus,
    write_run_record,
)
from src.net_env import normalize_proxy_env  # noqa: E402

API_PREFIX = "/api/literature"
PROJECT_PREFIX = "/api/project"


class DemoClientError(RuntimeError):
    """The demo could not even build its HTTP client."""


class DemoStepFailed(RuntimeError):
    """A demo step returned a failure the run must record, not hide."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _error_code(response: Any) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        return f"http_{response.status_code}"
    if isinstance(detail, Mapping) and isinstance(detail.get("code"), str):
        return str(detail["code"])
    if isinstance(detail, str) and detail:
        return detail
    return f"http_{response.status_code}"


def _search_plan(corpus: DemoCorpus) -> dict[str, Any]:
    return {
        "research_question": corpus.question,
        "suggested_query": corpus.confirmed_query,
        # The query is supplied by the caller, so it is recorded as user-confirmed
        # rather than as a generated suggestion.
        "generation_method": "user",
        "generation_model": None,
        "generation_config": {"source": "literature_demo_corpus"},
    }


def _query_body(corpus: DemoCorpus, *, page_size: int = 10) -> dict[str, Any]:
    return {
        "query": corpus.confirmed_query,
        "page": 1,
        "page_size": page_size,
        "sort_by": "relevance",
        "sort_order": "descending",
        "filters": {"year_from": None, "year_to": None, "categories": []},
    }


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return None
    commit = result.stdout.strip()
    return commit or None


class _Step:
    """Time one step and record its outcome exactly once."""

    def __init__(self, recorder: DemoRunRecorder, name: str) -> None:
        self._recorder = recorder
        self._name = name
        self._started = time.monotonic()
        self._counts: dict[str, int] = {}
        self._detail: dict[str, str] = {}

    def count(self, key: str, value: int) -> None:
        self._counts[key] = int(value)

    def detail(self, key: str, value: str) -> None:
        self._detail[key] = str(value)

    def _duration(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def ok(self) -> None:
        self._recorder.record(
            name=self._name,
            status=StepStatus.OK,
            duration_ms=self._duration(),
            counts=self._counts,
            detail=self._detail,
        )

    def failed(self, reason: str) -> None:
        self._recorder.record(
            name=self._name,
            status=StepStatus.FAILED,
            reason=reason,
            duration_ms=self._duration(),
            counts=self._counts,
            detail=self._detail,
        )

    def skipped(self, reason: str) -> None:
        self._recorder.record(
            name=self._name,
            status=StepStatus.SKIPPED,
            reason=reason,
            duration_ms=self._duration(),
            counts=self._counts,
            detail=self._detail,
        )


def run_demo(
    client: Any,
    *,
    project_path: str,
    provider: str,
    corpus: DemoCorpus,
    workspace: Path | None = None,
    write_records_to: Path | None = None,
    started_at: Any = None,
) -> DemoRun:
    """Run the fixed demo order once and return its record."""

    mode = "fixture" if provider.strip().casefold() == "fixture" else "live"
    recorder = DemoRunRecorder(
        mode=mode,
        provider=provider,
        project_path=project_path,
        expected_failures=corpus.expected_failures,
        confirmed_query=corpus.confirmed_query,
        question=corpus.question,
        started_at=started_at,
        git_commit=_git_commit(),
    )
    workspace = Path(workspace) if workspace is not None else Path.cwd() / "demo-workspace"

    # 1. search with the caller-confirmed query
    step = _Step(recorder, "search")
    response = client.post(
        f"{API_PREFIX}/search",
        json={
            "provider": provider,
            "plan": _search_plan(corpus),
            "query": _query_body(corpus),
        },
    )
    if response.status_code != 200:
        step.failed(_error_code(response))
        run = recorder.finish(answer_status=None)
        if write_records_to is not None:
            write_run_record(run, write_records_to)
        return run
    execution = response.json()
    records = execution["page"]["records"]
    step.count("records", len(records))
    step.detail("result_mode", str(execution["page"].get("result_mode", "")))
    step.ok()
    if not records:
        run = recorder.finish(answer_status=None)
        if write_records_to is not None:
            write_run_record(run, write_records_to)
        return run

    # 2. import the whole returned selection (the caller already chose it)
    step = _Step(recorder, "import")
    imported = client.post(
        f"{API_PREFIX}/import",
        json={
            "project_path": project_path,
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record["paper_id"] for record in records],
        },
    )
    if imported.status_code != 200:
        step.failed(_error_code(imported))
        run = recorder.finish(answer_status=None)
        if write_records_to is not None:
            write_run_record(run, write_records_to)
        return run
    batch = imported.json()
    source_by_paper = {item["paper_id"]: item["source_id"] for item in batch["results"]}
    step.count("created", batch["created_count"])
    step.count("reused", batch["reused_count"])
    step.ok()

    # 3. try to acquire the declared open full text; failures are recorded
    step = _Step(recorder, "acquire_fulltext")
    acquired: list[str] = []
    unavailable: list[str] = []
    acquire_failures: dict[str, str] = {}
    for source_id in source_by_paper.values():
        result = client.post(
            f"{API_PREFIX}/fulltext",
            json={"project_path": project_path, "source_id": source_id},
        )
        if result.status_code == 200:
            acquired.append(source_id)
        else:
            code = _error_code(result)
            acquire_failures[source_id] = code
            unavailable.append(source_id)
    step.count("requested", len(source_by_paper))
    step.count("acquired", len(acquired))
    step.count("unavailable", len(unavailable))
    if unavailable:
        first_reason = acquire_failures[unavailable[0]]
        step.failed(first_reason)
    else:
        step.ok()

    # 4. attach the local corpus full text where acquisition was unavailable
    step = _Step(recorder, "attach_fulltext")
    attached: list[str] = []
    if mode != "fixture":
        step.count("attached", 0)
        step.skipped("user_attachment_required")
    else:
        wanted = {record.paper_id for record in corpus.records_to_index()}
        for paper_id, source_id in source_by_paper.items():
            if paper_id not in wanted or source_id in acquired:
                continue
            pdf_path = materialize_pdf(
                corpus, paper_id, workspace / f"{paper_id.replace('/', '_')}.pdf"
            )
            with pdf_path.open("rb") as stream:
                response = client.post(
                    f"{PROJECT_PREFIX}/sources/import",
                    data={"project_path": project_path, "source_id": source_id},
                    files={"file": (pdf_path.name, stream, "application/pdf")},
                )
            if response.status_code == 200:
                attached.append(source_id)
            else:
                step.detail(f"attach_error_{source_id}", _error_code(response))
        step.count("attached", len(attached))
        if attached:
            step.ok()
        else:
            step.failed("no_local_fulltext_attached")

    # 5. build the page-level index for every source that has a full text
    step = _Step(recorder, "index")
    indexed: list[str] = []
    index_failures: dict[str, str] = {}
    for source_id in [*acquired, *attached]:
        response = client.post(
            f"{API_PREFIX}/index",
            json={"project_path": project_path, "source_id": source_id},
        )
        if response.status_code == 200:
            indexed.append(source_id)
        else:
            index_failures[source_id] = _error_code(response)
    step.count("indexed", len(indexed))
    step.count("failed", len(index_failures))
    if index_failures:
        # Report the real cause; "nothing to index" is a different situation.
        step.failed(next(iter(index_failures.values())))
    elif not indexed:
        step.failed("no_indexed_source")
    else:
        step.ok()

    # 6. answer inside the scope of the indexed sources
    step = _Step(recorder, "answer")
    answer_status: str | None = None
    answer_model_config_hash: str | None = None
    answer_payload: dict[str, Any] | None = None
    if not indexed:
        step.skipped("dependency_failed")
    else:
        response = client.post(
            f"{API_PREFIX}/answer",
            json={
                "project_path": project_path,
                "question": corpus.question,
                "source_ids": indexed,
                "top_k": 5,
            },
        )
        if response.status_code != 200:
            step.failed(_error_code(response))
        else:
            answer_payload = response.json()
            answer_status = str(answer_payload["status"])
            answer_model_config_hash = answer_payload.get("model_config_hash")
            step.count("claims", len(answer_payload["claims"]))
            step.count("evidence", len(answer_payload["evidence"]))
            step.count("rejected_claims", len(answer_payload["rejected_claims"]))
            step.ok()

    # 7. resolve one returned citation back through the evidence route
    step = _Step(recorder, "resolve_evidence")
    if not answer_payload or not answer_payload["evidence"]:
        step.skipped("no_evidence_to_resolve")
    else:
        item = answer_payload["evidence"][0]
        response = client.post(
            f"{API_PREFIX}/evidence",
            json={
                "project_path": project_path,
                "source_id": item["source_id"],
                "chunk_id": item["chunk_id"],
            },
        )
        if response.status_code != 200:
            step.failed(_error_code(response))
        else:
            span = response.json()["span"]
            step.count("page", int(span["page_start"]))
            step.detail("coordinate_space", str(span["coordinate_space"]))
            step.count("quote_chars", len(span["exact_quote"]))
            step.ok()

    run = recorder.finish(
        answer_status=answer_status,
        answer_model_config_hash=answer_model_config_hash,
    )
    if write_records_to is not None:
        write_run_record(run, write_records_to)
    return run


def _http_client(base_url: str) -> Any:
    import httpx

    return httpx.Client(base_url=base_url, timeout=120.0)


def _build_client(factory, base_url: str) -> Any:
    """Build the HTTP client, turning a broken proxy setup into a clear error."""

    try:
        return factory(base_url)
    except Exception as exc:  # noqa: BLE001 - the CLI must explain, not traceback
        raise DemoClientError(f"{type(exc).__name__}: {exc}") from exc


def main(argv: Sequence[str] | None = None, *, client_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed literature PoC demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:18088")
    parser.add_argument("--project-path", default=None, help="an existing project directory")
    parser.add_argument(
        "--create-location",
        default=None,
        help="create the project under this directory instead of using --project-path",
    )
    parser.add_argument("--project-name", default="Literature PoC Demo")
    parser.add_argument("--provider", default="fixture", choices=["fixture", "arxiv"])
    parser.add_argument("--corpus", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--records-dir",
        default=str(Path(__file__).resolve().parents[2] / "methods" / "literature_poc" / "runs"),
    )
    args = parser.parse_args(argv)

    if bool(args.project_path) == bool(args.create_location):
        print(
            json.dumps(
                {
                    "error": "project_target_required",
                    "message": "pass exactly one of --project-path or --create-location",
                },
                ensure_ascii=False,
            )
        )
        return 4

    try:
        corpus = load_demo_corpus(args.corpus)
    except DemoCorpusError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 2

    # A bracketed IPv6 entry in NO_PROXY makes httpx fail while building a client.
    normalize_proxy_env()

    factory = client_factory or _http_client
    try:
        client = _build_client(factory, args.base_url)
    except DemoClientError as exc:
        print(json.dumps({"error": "client_unavailable", "message": str(exc)}, ensure_ascii=False))
        return 3

    with client:
        project_path, error = _resolve_project(client, args)
        if error is not None:
            print(json.dumps(error, ensure_ascii=False))
            return 4
        run = run_demo(
            client,
            project_path=project_path,
            provider=args.provider,
            corpus=corpus,
            workspace=Path(args.workspace) if args.workspace else None,
            write_records_to=Path(args.records_dir),
        )
    print(json.dumps(json.loads(run.model_dump_json()), ensure_ascii=False, indent=2))
    # A failure the corpus declared is still a failure in the record; it just is
    # not a regression, so it does not make the run exit non-zero.
    unexpected = [
        step
        for step in run.steps
        if step.status is StepStatus.FAILED and step.detail.get("expected") != "true"
    ]
    return 1 if unexpected else 0


def _resolve_project(client: Any, args: argparse.Namespace) -> tuple[str, dict | None]:
    """Return the project to run against, creating one when asked.

    The demo writes into an existing project's source library, so a target that is
    not a project must fail up front with a clear message instead of surfacing as a
    confusing ``import`` failure.
    """

    if args.create_location:
        created = client.post(
            f"{PROJECT_PREFIX}/create",
            json={
                "name": args.project_name,
                "location": str(args.create_location),
                "template_id": "research_paper",
                "init_git": False,
            },
        )
        if created.status_code != 200:
            return "", {
                "error": _error_code(created),
                "message": f"could not create a project under {args.create_location}",
            }
        return str(created.json()["project_path"]), None

    project_path = str(args.project_path)
    probe = client.get(f"{PROJECT_PREFIX}/sources", params={"project_path": project_path})
    if probe.status_code != 200:
        return "", {
            "error": "project_not_found",
            "message": f"{project_path} is not a project directory",
            "hint": "create one in the app, or pass --create-location",
        }
    return project_path, None


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
