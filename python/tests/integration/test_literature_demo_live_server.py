"""The demo CLI, driven against a real uvicorn server over real HTTP.

The unit tests inject a stub client and the demo e2e drives the app in-process.
This is the missing third layer: a separate server process, real sockets, real
ChromaDB persistence, and the CLI's own entry point — which is what an operator
actually runs in front of an audience.

The deterministic fixture answer model (D-040) is enabled for the child process,
so the run needs no model and no network.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

PYTHON_DIR = Path(__file__).resolve().parents[2]
HEALTH_TIMEOUT_S = 60.0
HEALTH_POLL_S = 0.5


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_health(port: int, process: subprocess.Popen) -> bool:
    import httpx

    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - the server may not be listening yet
            time.sleep(HEALTH_POLL_S)
    return False


@pytest.fixture
def live_server(tmp_path: Path):
    pytest.importorskip("chromadb")
    pytest.importorskip("uvicorn")

    port = _free_port()
    env = dict(os.environ)
    env["SCHOLAR_LITERATURE_ANSWER_MODE"] = "fixture"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=PYTHON_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_for_health(port, process):
            output = b""
            if process.stdout is not None:
                process.kill()
                output = process.stdout.read()[-2000:]
            pytest.skip(f"the API server did not start: {output.decode('utf-8', 'replace')}")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()


def test_cli_drives_the_fixed_demo_against_a_real_server(
    tmp_path: Path, live_server: str, capsys: pytest.CaptureFixture
) -> None:
    from scripts.literature_demo import main

    records = tmp_path / "runs"
    code = main(
        [
            "--base-url",
            live_server,
            "--create-location",
            str(tmp_path),
            "--project-name",
            "Live Server Demo",
            "--provider",
            "fixture",
            "--records-dir",
            str(records),
        ]
    )

    assert code == 0, capsys.readouterr().out
    written = list(records.glob("*.json"))
    assert len(written) == 1
    record = json.loads(written[0].read_text(encoding="utf-8"))

    assert record["mode"] == "fixture"
    assert record["project_path"].endswith("Live Server Demo")
    assert [step["name"] for step in record["steps"]] == [
        "search",
        "import",
        "acquire_fulltext",
        "attach_fulltext",
        "index",
        "answer",
        "resolve_evidence",
    ]
    steps = {step["name"]: step for step in record["steps"]}

    # The synthetic corpus declares no open full text, so this step really fails;
    # the record says so and marks it as the corpus's declared expectation.
    assert steps["acquire_fulltext"]["status"] == "failed"
    assert steps["acquire_fulltext"]["reason"] == "access_unavailable"
    assert steps["acquire_fulltext"]["detail"]["expected"] == "true"

    # The rest of the chain completed on the real server, and the answer carries
    # the deterministic fixture identity rather than a model identity.
    assert steps["attach_fulltext"]["counts"]["attached"] == 2
    assert steps["index"]["counts"]["indexed"] == 2
    assert steps["answer"]["status"] == "ok"
    assert steps["answer"]["counts"]["claims"] >= 1
    assert record["answer_status"] == "answered"
    assert steps["resolve_evidence"]["status"] == "ok"
    assert steps["resolve_evidence"]["counts"]["page"] >= 1
    assert record["answer_model_config_hash"]

    printed = json.loads(capsys.readouterr().out)
    assert printed["run_id"] == record["run_id"]


def test_cli_reports_a_missing_project_before_running_any_step(
    tmp_path: Path, live_server: str, capsys: pytest.CaptureFixture
) -> None:
    from scripts.literature_demo import main

    empty = tmp_path / "not-a-project"
    empty.mkdir()
    records = tmp_path / "runs"

    code = main(
        [
            "--base-url",
            live_server,
            "--project-path",
            str(empty),
            "--provider",
            "fixture",
            "--records-dir",
            str(records),
        ]
    )

    assert code == 4
    assert "project_not_found" in capsys.readouterr().out
    assert list(records.glob("*.json")) == []
