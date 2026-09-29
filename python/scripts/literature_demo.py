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

API_PREFIX = "/api/literature"
PROJECT_PREFIX = "/api/project"


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed literature PoC demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:18088")
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--provider", default="fixture", choices=["fixture", "arxiv"])
    parser.add_argument("--corpus", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--records-dir",
        default=str(Path(__file__).resolve().parents[2] / "methods" / "literature_poc" / "runs"),
    )
    args = parser.parse_args(argv)

    try:
        corpus = load_demo_corpus(args.corpus)
    except DemoCorpusError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 2

    with _http_client(args.base_url) as client:
        run = run_demo(
            client,
            project_path=args.project_path,
            provider=args.provider,
            corpus=corpus,
            workspace=Path(args.workspace) if args.workspace else None,
            write_records_to=Path(args.records_dir),
        )
    print(json.dumps(json.loads(run.model_dump_json()), ensure_ascii=False, indent=2))
    return 0 if run.totals["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
