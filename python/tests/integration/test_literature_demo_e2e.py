"""P4 end-to-end test: the same demo script in cached and online mode.

Scope (frozen in docs/literature-research-poc-plan.md section 5.10): the real
application must run the fixed demo order

    search -> import -> acquire open full text -> attach local full text
      -> page index -> scoped evidence answer -> resolve one citation

in **both** modes — the offline ``fixture`` provider shipped with the demo corpus
(M10) and a live-named provider — with the same step structure, a verifiable page
quote at the end, and failures recorded as failures rather than successes.

Everything is offline: the metadata provider, the downloader and the answering
model are substituted before ``create_app`` runs (same pattern as D-024), while
the project store, PDF parsing, ChromaDB persistence, scope filters and evidence
resolution stay on the production path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from scripts.literature_demo import run_demo  # noqa: E402
from src.literature.demo_corpus import (  # noqa: E402
    DemoCorpus,
    load_demo_corpus,
    materialize_pdf,
)
from src.literature.fulltext import DownloadedArtifact  # noqa: E402
from src.literature.models import (  # noqa: E402
    AccessLocation,
    PaperRecord,
    ProviderCapabilities,
    SearchPage,
    SearchResultMode,
)
from src.literature.providers.base import (  # noqa: E402
    LiteratureProviderError,
    ProviderErrorCode,
    ProviderOperation,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
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
  max_stalled_tool_calls: 3
"""


class OfflineArxivStub:
    """Contract-compatible stand-in for ``ArxivProvider`` that never hits the net."""

    name = "arxiv"

    def __init__(self, corpus: DemoCorpus, *, open_fulltext: bool) -> None:
        self._corpus = corpus
        self._by_paper_id: dict[str, PaperRecord] = {}
        ordered: list[PaperRecord] = []
        wanted = set(corpus.search_results[corpus.confirmed_query])
        for record in corpus.records:
            payload = record.model_dump(mode="json")
            # The stub owns the records it returns, so the provider identity and
            # the derived paper_id must match its own name, not the corpus's.
            payload["provider"] = self.name
            payload["paper_id"] = ""
            payload["metadata_snapshot_hash"] = ""
            payload["access_locations"] = (
                [
                    {
                        "kind": "pdf",
                        "url": f"https://arxiv.org/pdf/{record.provider_record_id}",
                        "access_status": "open",
                        "mime_type": "application/pdf",
                        "license": None,
                        "is_primary": True,
                    }
                ]
                if open_fulltext
                else []
            )
            validated = PaperRecord.model_validate(payload)
            if record.paper_id in wanted:
                ordered.append(validated)
            self._by_paper_id[validated.paper_id] = validated
        self._order = ordered

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            result_mode=SearchResultMode.LIVE,
            supports_fulltext_download=True,
        )

    async def search(self, query) -> SearchPage:
        records = list(self._order)
        start = (query.page - 1) * query.page_size
        page_records = records[start : start + query.page_size]
        return SearchPage(
            provider=self.name,
            result_mode=SearchResultMode.LIVE,
            query=query,
            records=page_records,
            total_results=len(records),
            has_more=start + len(page_records) < len(records),
            retrieved_at=NOW,
            provenance_label="offline-arxiv-stub",
        )

    async def get_record(self, external_id: str) -> PaperRecord:
        raise LiteratureProviderError(
            ProviderErrorCode.NOT_FOUND,
            f"offline stub has no record {external_id}",
            provider=self.name,
            operation=ProviderOperation.GET_RECORD,
        )

    async def resolve_access(self, record: PaperRecord) -> Sequence[AccessLocation]:
        configured = self._by_paper_id.get(record.paper_id)
        return list(configured.access_locations) if configured else []


class CorpusDownloader:
    """Serve the corpus PDF for the declared open location, fully offline."""

    def __init__(self, corpus: DemoCorpus, workspace: Path) -> None:
        self._corpus = corpus
        self._workspace = workspace
        self.urls: list[str] = []

    async def download(self, url: str) -> DownloadedArtifact:
        self.urls.append(url)
        provider_record_id = url.rsplit("/", 1)[-1]
        record = self._corpus.record_for_provider_id(provider_record_id)
        if record is None:
            raise ValueError(f"unknown demo record in url: {url}")
        pdf = materialize_pdf(
            self._corpus, record.paper_id, self._workspace / f"{provider_record_id}.pdf"
        )
        content = pdf.read_bytes()
        return DownloadedArtifact(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            mime_type="application/pdf",
            source_url=url,
        )

    async def aclose(self) -> None:
        return None


class ScriptedAnswerProvider:
    """Deterministic answering model: cite every offered evidence id."""

    provider_name = "scripted"
    model = "scripted-answer-v1"
    base_url = ""

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        import re

        from src.agent_v2.types import ProviderResponse, TextBlock

        prompt = messages[0].text_content() if messages else ""
        if "UNANSWERABLE" in prompt:
            payload = {
                "claims": [
                    {
                        "text": "The selected corpus does not answer this question.",
                        "evidence_ids": [],
                        "evidence_status": "insufficient",
                    }
                ]
            }
        else:
            evidence_ids = re.findall(r"\[(evidence_[0-9a-f]{24})\]", prompt)
            payload = {
                "claims": [
                    {
                        "text": "The selected demo papers report this evaluation protocol.",
                        "evidence_ids": evidence_ids,
                        "evidence_status": "supported",
                    }
                ]
            }
        return ProviderResponse(blocks=[TextBlock(text=json.dumps(payload))])


@pytest.fixture(scope="module")
def corpus() -> DemoCorpus:
    return load_demo_corpus()


@pytest.fixture(scope="module")
def client(corpus: DemoCorpus) -> Iterator[TestClient]:
    pytest.importorskip("chromadb")
    from api_factory import create_app

    test_dir = Path(tempfile.mkdtemp(prefix="p4-demo-"))
    config_dir = test_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "default.yaml").write_text(CONFIG, encoding="utf-8")

    stub = OfflineArxivStub(corpus, open_fulltext=True)
    downloader = CorpusDownloader(corpus, test_dir / "downloads")
    answer_provider = ScriptedAnswerProvider()

    with (
        patch("api_factory.CONFIG_PATH", config_dir / "default.yaml"),
        patch("api_factory.RUNTIME_DIR", test_dir),
        patch("api_factory.BASE_DIR", test_dir),
        patch("src.literature.providers.arxiv.ArxivProvider", lambda *a, **k: stub),
        patch("src.literature.fulltext.HttpFullTextDownloader", lambda *a, **k: downloader),
        patch("src.agent_v2.router._create_provider", lambda *a, **k: answer_provider),
    ):
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client

    shutil.rmtree(test_dir, ignore_errors=True)


def create_project(client: TestClient, workdir: Path) -> str:
    location = workdir / "projects"
    location.mkdir(parents=True, exist_ok=True)
    created = client.post(
        "/api/project/create",
        json={
            "name": "P4 Demo",
            "location": str(location),
            "template_id": "research_paper",
            "init_git": False,
        },
    )
    assert created.status_code == 200, created.text
    return str(created.json()["project_path"])


def test_demo_corpus_is_registered_as_an_offline_provider(client: TestClient) -> None:
    """M10: the shipping app exposes the cached provider next to the live one."""

    providers = client.get("/api/literature/providers")
    assert providers.status_code == 200
    payload = {item["provider"]: item for item in providers.json()["providers"]}

    assert "arxiv" in payload
    assert "fixture" in payload
    assert payload["fixture"]["result_mode"] == "fixture"
    assert payload["fixture"]["supports_fulltext_download"] is False


def test_same_demo_script_runs_in_cached_and_online_mode(
    client: TestClient, corpus: DemoCorpus
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="p4-demo-run-"))
    records_dir = workdir / "runs"
    try:
        cached_project = create_project(client, workdir / "cached")
        cached = run_demo(
            client,
            project_path=cached_project,
            provider="fixture",
            corpus=corpus,
            workspace=workdir / "cached-workspace",
            write_records_to=records_dir,
        )

        online_project = create_project(client, workdir / "online")
        online = run_demo(
            client,
            project_path=online_project,
            provider="arxiv",
            corpus=corpus,
            workspace=workdir / "online-workspace",
            write_records_to=records_dir,
        )

        # The same order every time: that is what makes the two modes comparable.
        assert [step.name for step in cached.steps] == [
            "search",
            "import",
            "acquire_fulltext",
            "attach_fulltext",
            "index",
            "answer",
            "resolve_evidence",
        ]
        assert [step.name for step in online.steps] == [step.name for step in cached.steps]

        def state(run) -> list[tuple[str, str]]:
            return [(step.name, step.status.value) for step in run.steps]

        assert state(cached) == [
            ("search", "ok"),
            ("import", "ok"),
            ("acquire_fulltext", "failed"),
            ("attach_fulltext", "ok"),
            ("index", "ok"),
            ("answer", "ok"),
            ("resolve_evidence", "ok"),
        ]
        assert state(online) == [
            ("search", "ok"),
            ("import", "ok"),
            ("acquire_fulltext", "ok"),
            ("attach_fulltext", "skipped"),
            ("index", "ok"),
            ("answer", "ok"),
            ("resolve_evidence", "ok"),
        ]

        # The cached run must reach a real page quote through the local fallback.
        cached_acquire = next(s for s in cached.steps if s.name == "acquire_fulltext")
        assert cached_acquire.reason == "access_unavailable"
        assert cached_acquire.counts["unavailable"] == 3

        for run in (cached, online):
            assert run.answer_status == "answered"
            evidence_step = next(s for s in run.steps if s.name == "resolve_evidence")
            assert evidence_step.counts["page"] >= 1
            assert evidence_step.counts["quote_chars"] > 0
            assert evidence_step.detail["coordinate_space"] == "normalized_page_text_v1"
            assert run.totals["failed"] == (1 if run is cached else 0)
            assert run.totals["step"] == 7

        # Run records are written per run and never carry a secret.
        written = sorted(records_dir.glob("*.json"))
        assert len(written) == 2
        for path in written:
            body = json.loads(path.read_text(encoding="utf-8"))
            flattened = json.dumps(body, ensure_ascii=False).casefold()
            assert "api_key" not in flattened
            assert "authorization" not in flattened
            assert body["totals"]["step"] == 7
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_repeating_the_cached_demo_is_field_by_field_comparable(
    client: TestClient, corpus: DemoCorpus
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="p4-demo-repeat-"))
    try:
        first = run_demo(
            client,
            project_path=create_project(client, workdir / "first"),
            provider="fixture",
            corpus=corpus,
            workspace=workdir / "first-workspace",
        )
        second = run_demo(
            client,
            project_path=create_project(client, workdir / "second"),
            provider="fixture",
            corpus=corpus,
            workspace=workdir / "second-workspace",
        )

        assert first.run_id == second.run_id
        assert [step.name for step in first.steps] == [step.name for step in second.steps]
        assert [step.status for step in first.steps] == [step.status for step in second.steps]
        assert [step.reason for step in first.steps] == [step.reason for step in second.steps]
        assert first.totals == second.totals
        assert first.answer_status == second.answer_status == "answered"
        # A repeat run is still a real run: it re-searches and re-answers.
        assert first.duration_ms >= 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_demo_records_the_unanswered_case_instead_of_inventing_an_answer(
    client: TestClient, corpus: DemoCorpus
) -> None:
    """An unanswerable question is a successful run with an explicit insufficiency."""

    workdir = Path(tempfile.mkdtemp(prefix="p4-demo-unanswerable-"))
    try:
        project = create_project(client, workdir)
        unanswerable = DemoCorpus(
            corpus_id=corpus.corpus_id,
            kind=corpus.kind,
            provenance=corpus.provenance,
            confirmed_query=corpus.confirmed_query,
            question="UNANSWERABLE: what is the airspeed of a swallow?",
            records=corpus.records,
            search_results=corpus.search_results,
            pages=corpus.pages,
            index_paper_ids=corpus.index_paper_ids,
            access_locations=corpus.access_locations,
        )

        run = run_demo(
            client,
            project_path=project,
            provider="fixture",
            corpus=unanswerable,
            workspace=workdir / "workspace",
        )
        answer_step = next(step for step in run.steps if step.name == "answer")
        assert answer_step.status.value == "ok"
        assert run.answer_status == "insufficient"
        assert answer_step.counts["claims"] == 0
        evidence_step = next(step for step in run.steps if step.name == "resolve_evidence")
        assert evidence_step.status.value == "skipped"
        assert evidence_step.reason == "no_evidence_to_resolve"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
