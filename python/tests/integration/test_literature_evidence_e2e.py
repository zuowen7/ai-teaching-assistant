"""P2B end-to-end test: research question to verified page quote.

Scope (frozen in docs/literature-research-poc-plan.md section 5.7): the real
application (``create_app`` + ``TestClient``) must carry one literature source
from discovery to a machine-checkable citation, fully offline:

    providers -> search -> project import -> attach local PDF -> page index
      -> scoped retrieval -> evidence resolution

The only substituted part is the metadata provider: the shipping app uses
``ArxivProvider``, which needs the network, so the test patches that class with
the deterministic ``FixtureProvider`` before the app is created.  Everything
else — project store, PDF parsing, ChromaDB persistence, scope filters, evidence
resolution — is the production code path.

Requires a working chromadb installation; skipped explicitly when it is missing.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.literature.evidence import normalize_page_text, sha256_file  # noqa: E402
from src.literature.fulltext import DownloadedArtifact  # noqa: E402
from src.literature.models import (  # noqa: E402
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    PaperRecord,
)
from src.literature.providers.fixture import FixtureProvider  # noqa: E402

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
  max_stalled_tool_calls: 3
"""

NOW = datetime(2026, 9, 22, 4, 0, tzinfo=UTC)
QUERY = 'all:"retrieval augmented generation"'
RESEARCH_QUESTION = "How does retrieval augmented generation ground answers?"
PAGE_ONE = (
    "Retrieval augmented generation grounds answers in retrieved passages.\n\n"
    "This first page states the problem statement of the study."
)
PAGE_TWO = (
    "The second page reports the evaluation protocol and its metrics.\n\n"
    "Only this page mentions the held-out evaluation split."
)
PAGE_TWO_MARKER = "held-out evaluation split"


def make_record() -> PaperRecord:
    return PaperRecord(
        provider="fixture",
        provider_record_id="cs/9901001",
        external_ids=ExternalIdentifiers(doi="10.1000/e2e", arxiv="cs/9901001v2"),
        title="Multi-Agent Writing Assistance",
        authors=["Ada Researcher", "Lin Reviewer"],
        year=2026,
        venue="PoC Proceedings",
        abstract="A deterministic record for the end-to-end verification.",
        categories=["cs.AI"],
        record_url="https://arxiv.org/abs/cs/9901001v2",
        access_locations=[
            AccessLocation(
                kind=AccessKind.PDF,
                url="https://arxiv.org/pdf/cs/9901001v2",
                access_status=AccessStatus.OPEN,
                mime_type="application/pdf",
                is_primary=True,
            )
        ],
        source_query=QUERY,
        retrieved_at=NOW,
    )


def make_closed_record() -> PaperRecord:
    """A record whose provider declares no open full text at all."""

    payload = make_record().model_dump(mode="json")
    payload["provider_record_id"] = "cs/9901002"
    payload["external_ids"] = {"doi": "10.1000/e2e-closed", "arxiv": "cs/9901002"}
    payload["title"] = "Restricted Access Paper"
    payload["record_url"] = "https://arxiv.org/abs/cs/9901002"
    payload["access_locations"] = []
    # paper_id and the snapshot hash are both derived from the fields above.
    payload["paper_id"] = ""
    payload["metadata_snapshot_hash"] = ""
    return PaperRecord.model_validate(payload)


class StubDownloader:
    """Offline downloader used instead of the shipping httpx implementation."""

    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path
        self.urls: list[str] = []

    async def download(self, url: str) -> DownloadedArtifact:
        self.urls.append(url)
        content = self.pdf_path.read_bytes()
        return DownloadedArtifact(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            mime_type="application/pdf",
            source_url=url,
        )

    async def aclose(self) -> None:
        return None


EVIDENCE_ID_RE = re.compile(r"\[(evidence_[0-9a-f]{24})\]")
UNANSWERABLE_MARKER = "UNANSWERABLE"


class ScriptedAnswerProvider:
    """Offline stand-in for the Agent provider behind the answer route.

    It is injected through ``_create_provider`` before ``create_app`` runs, the
    same way the metadata provider and the downloader are replaced (D-024).
    """

    provider_name = "scripted"
    model = "scripted-answer-v1"
    base_url = ""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat(
        self,
        messages,
        tools=None,
        system_prompt=None,
        max_tokens=4096,
        temperature=0.3,
        tool_choice="auto",
    ):
        from src.agent_v2.types import ProviderResponse, TextBlock

        prompt = messages[0].text_content() if messages else ""
        self.prompts.append(prompt)
        if UNANSWERABLE_MARKER in prompt:
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
            payload = {
                "claims": [
                    {
                        "text": "The selected paper reports this finding.",
                        "evidence_ids": EVIDENCE_ID_RE.findall(prompt),
                        "evidence_status": "supported",
                    }
                ]
            }
        return ProviderResponse(blocks=[TextBlock(text=json.dumps(payload))])


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    import fitz

    document = fitz.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 96), text, fontsize=11)
    document.save(str(path))
    document.close()
    return path


def parsed_pages(path: Path) -> dict[int, str]:
    from src.parser import extract_document

    document = extract_document(path)
    return {
        page.page_num: normalize_page_text(page.text)
        for page in document.pages
        if normalize_page_text(page.text)
    }


@pytest.fixture(scope="module")
def downloader() -> StubDownloader:
    workdir = Path(tempfile.mkdtemp(prefix="p2b-e2e-download-"))
    stub = StubDownloader(write_pdf(workdir / "open.pdf", [PAGE_ONE, PAGE_TWO]))
    yield stub
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="module")
def answer_provider() -> ScriptedAnswerProvider:
    return ScriptedAnswerProvider()


@pytest.fixture(scope="module")
def client(
    downloader: StubDownloader, answer_provider: ScriptedAnswerProvider
) -> Iterator[TestClient]:
    pytest.importorskip("chromadb")
    from api_factory import create_app

    test_dir = Path(tempfile.mkdtemp(prefix="p2b-e2e-"))
    config_dir = test_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "default.yaml").write_text(CONFIG, encoding="utf-8")

    open_record = make_record()
    closed_record = make_closed_record()
    provider = FixtureProvider(
        fixture_name="e2e-fixture-v1",
        snapshot_at=NOW,
        records=[open_record, closed_record],
        search_results={QUERY: [open_record.paper_id, closed_record.paper_id]},
    )

    with (
        patch("api_factory.CONFIG_PATH", config_dir / "default.yaml"),
        patch("api_factory.RUNTIME_DIR", test_dir),
        patch("api_factory.BASE_DIR", test_dir),
        patch("src.literature.providers.arxiv.ArxivProvider", lambda *a, **k: provider),
        patch("src.literature.fulltext.HttpFullTextDownloader", lambda *a, **k: downloader),
        patch("src.agent_v2.router._create_provider", lambda *a, **k: answer_provider),
    ):
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client

    shutil.rmtree(test_dir, ignore_errors=True)


def test_research_question_to_verified_page_quote(client: TestClient) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="p2b-e2e-project-"))
    try:
        location = workdir / "projects"
        location.mkdir(parents=True, exist_ok=True)

        created = client.post(
            "/api/project/create",
            json={
                "name": "P2B EndToEnd",
                "location": str(location),
                "template_id": "research_paper",
                "init_git": False,
            },
        )
        assert created.status_code == 200, created.text
        project = Path(created.json()["project_path"])

        providers = client.get("/api/literature/providers")
        assert providers.status_code == 200
        assert providers.json()["providers"][0]["provider"] == "fixture"

        searched = client.post(
            "/api/literature/search",
            json={
                "provider": "fixture",
                "query": {
                    "query": QUERY,
                    "page": 1,
                    "page_size": 10,
                    "sort_by": "relevance",
                    "sort_order": "descending",
                    "filters": {"year_from": None, "year_to": None, "categories": []},
                },
                "plan": {
                    "research_question": RESEARCH_QUESTION,
                    "suggested_query": QUERY,
                    "generation_method": "template",
                    "generation_model": None,
                    "generation_config": {"template": "arxiv_all_phrase_v1"},
                },
            },
        )
        assert searched.status_code == 200, searched.text
        execution = searched.json()
        paper_id = execution["page"]["records"][0]["paper_id"]

        imported = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution["search_execution_id"],
                "paper_ids": [paper_id],
            },
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["created_count"] == 1
        source_id = imported.json()["results"][0]["source_id"]

        pdf = write_pdf(workdir / "attached.pdf", [PAGE_ONE, PAGE_TWO])
        with pdf.open("rb") as stream:
            attached = client.post(
                "/api/project/sources/import",
                data={"project_path": str(project), "source_id": source_id},
                files={"file": (pdf.name, stream, "application/pdf")},
            )
        assert attached.status_code == 200, attached.text

        indexed = client.post(
            "/api/literature/index",
            json={"project_path": str(project), "source_id": source_id},
        )
        assert indexed.status_code == 200, indexed.text
        index = indexed.json()
        assert index["status"] == "indexed"
        assert index["page_count"] == 2
        assert index["artifact_sha256"] == sha256_file(pdf)

        retrieved = client.post(
            "/api/rag/query",
            json={
                "query": PAGE_TWO_MARKER,
                "top_k": 5,
                "project_root": str(project),
                "source_ids": [source_id],
                "project_scoped": True,
            },
        )
        assert retrieved.status_code == 200, retrieved.text
        hits = retrieved.json()["hits"]
        assert hits, "scoped retrieval returned no hits for an indexed document"
        page_two_hits = [hit for hit in hits if hit["metadata"]["page_start"] == 2]
        assert page_two_hits, "no hit resolved to the page that contains the phrase"

        hit = page_two_hits[0]
        resolved = client.post(
            "/api/literature/evidence",
            json={
                "project_path": str(project),
                "source_id": source_id,
                "chunk_id": hit["chunk_id"],
            },
        )
        assert resolved.status_code == 200, resolved.text
        span = resolved.json()["span"]

        assert span["page_start"] == span["page_end"] == 2
        assert span["artifact_sha256"] == index["artifact_sha256"]
        assert span["coordinate_space"] == "normalized_page_text_v1"
        assert span["exact_quote"] == hit["text"]
        assert PAGE_TWO_MARKER in span["exact_quote"]
        assert parsed_pages(pdf)[2][span["char_start"] : span["char_end"]] == span["exact_quote"]
        assert resolved.json()["paper_id"] == paper_id

        unscoped = client.post(
            "/api/rag/query",
            json={"query": PAGE_TWO_MARKER, "project_scoped": True},
        )
        assert unscoped.status_code == 400

        sources = client.get("/api/project/sources", params={"project_path": str(project)}).json()
        literature = sources["sources"][0]["metadata"]["literature"]
        assert literature["fulltext"]["status"] == "indexed"
        assert literature["index"]["artifact_sha256"] == index["artifact_sha256"]
        assert literature["index"]["chunk_count"] == index["chunk_count"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_acquired_open_pdf_reaches_verified_evidence(
    client: TestClient,
    downloader: StubDownloader,
) -> None:
    """M5: the app fetches the declared open PDF and cites it, fully offline."""

    workdir = Path(tempfile.mkdtemp(prefix="p2b-e2e-acquire-"))
    try:
        location = workdir / "projects"
        location.mkdir(parents=True, exist_ok=True)
        created = client.post(
            "/api/project/create",
            json={
                "name": "M5 EndToEnd",
                "location": str(location),
                "template_id": "research_paper",
                "init_git": False,
            },
        )
        assert created.status_code == 200, created.text
        project = Path(created.json()["project_path"])

        execution = client.post(
            "/api/literature/search",
            json={
                "provider": "fixture",
                "query": {
                    "query": QUERY,
                    "page": 1,
                    "page_size": 10,
                    "sort_by": "relevance",
                    "sort_order": "descending",
                    "filters": {"year_from": None, "year_to": None, "categories": []},
                },
                "plan": {
                    "research_question": RESEARCH_QUESTION,
                    "suggested_query": QUERY,
                    "generation_method": "template",
                    "generation_model": None,
                    "generation_config": {"template": "arxiv_all_phrase_v1"},
                },
            },
        )
        assert execution.status_code == 200, execution.text
        records = execution.json()["page"]["records"]
        open_paper_id = next(
            record["paper_id"] for record in records if record["title"].startswith("Multi-Agent")
        )
        closed_paper_id = next(
            record["paper_id"] for record in records if record["title"].startswith("Restricted")
        )

        imported = client.post(
            "/api/literature/import",
            json={
                "project_path": str(project),
                "search_execution_id": execution.json()["search_execution_id"],
                "paper_ids": [open_paper_id, closed_paper_id],
            },
        )
        assert imported.status_code == 200, imported.text
        by_paper = {item["paper_id"]: item["source_id"] for item in imported.json()["results"]}
        open_source_id = by_paper[open_paper_id]
        closed_source_id = by_paper[closed_paper_id]

        closed = client.post(
            "/api/literature/fulltext",
            json={"project_path": str(project), "source_id": closed_source_id},
        )
        assert closed.status_code == 409
        assert closed.json()["detail"]["code"] == "access_unavailable"

        acquired = client.post(
            "/api/literature/fulltext",
            json={"project_path": str(project), "source_id": open_source_id},
        )
        assert acquired.status_code == 200, acquired.text
        body = acquired.json()
        assert body["status"] == "fulltext_ready"
        assert body["source_url"] == "https://arxiv.org/pdf/cs/9901001v2"
        local_path = Path(body["local_path"])
        assert local_path.is_file()
        assert local_path.parent == (project / "references").resolve()
        assert body["sha256"] == sha256_file(local_path)

        indexed = client.post(
            "/api/literature/index",
            json={"project_path": str(project), "source_id": open_source_id},
        )
        assert indexed.status_code == 200, indexed.text
        assert indexed.json()["artifact_sha256"] == body["sha256"]

        retrieved = client.post(
            "/api/rag/query",
            json={
                "query": PAGE_TWO_MARKER,
                "top_k": 5,
                "project_root": str(project),
                "source_ids": [open_source_id],
                "project_scoped": True,
            },
        )
        assert retrieved.status_code == 200, retrieved.text
        hits = retrieved.json()["hits"]
        assert hits
        hit = next(item for item in hits if item["metadata"]["page_start"] == 2)

        resolved = client.post(
            "/api/literature/evidence",
            json={
                "project_path": str(project),
                "source_id": open_source_id,
                "chunk_id": hit["chunk_id"],
            },
        )
        assert resolved.status_code == 200, resolved.text
        span = resolved.json()["span"]
        assert span["page_start"] == span["page_end"] == 2
        assert PAGE_TWO_MARKER in span["exact_quote"]
        assert (
            parsed_pages(local_path)[2][span["char_start"] : span["char_end"]]
            == span["exact_quote"]
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def prepare_indexed_project(client: TestClient, workdir: Path, name: str) -> tuple[Path, str, Path]:
    """Run the demo chain up to a page-indexed source and return its identity."""

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
    project = Path(created.json()["project_path"])

    searched = client.post(
        "/api/literature/search",
        json={
            "provider": "fixture",
            "query": {
                "query": QUERY,
                "page": 1,
                "page_size": 10,
                "sort_by": "relevance",
                "sort_order": "descending",
                "filters": {"year_from": None, "year_to": None, "categories": []},
            },
            "plan": {
                "research_question": RESEARCH_QUESTION,
                "suggested_query": QUERY,
                "generation_method": "template",
                "generation_model": None,
                "generation_config": {"template": "arxiv_all_phrase_v1"},
            },
        },
    )
    assert searched.status_code == 200, searched.text
    execution = searched.json()
    paper_id = execution["page"]["records"][0]["paper_id"]

    imported = client.post(
        "/api/literature/import",
        json={
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [paper_id],
        },
    )
    assert imported.status_code == 200, imported.text
    source_id = imported.json()["results"][0]["source_id"]

    pdf = write_pdf(workdir / "answer.pdf", [PAGE_ONE, PAGE_TWO])
    with pdf.open("rb") as stream:
        attached = client.post(
            "/api/project/sources/import",
            data={"project_path": str(project), "source_id": source_id},
            files={"file": (pdf.name, stream, "application/pdf")},
        )
    assert attached.status_code == 200, attached.text

    indexed = client.post(
        "/api/literature/index",
        json={"project_path": str(project), "source_id": source_id},
    )
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["status"] == "indexed"
    return project, source_id, pdf


def test_scoped_answer_cites_a_verified_page_quote(
    client: TestClient,
    answer_provider: ScriptedAnswerProvider,
) -> None:
    """P3: the demo chain ends in an answer whose every citation is verifiable."""

    workdir = Path(tempfile.mkdtemp(prefix="p3-e2e-answer-"))
    try:
        project, source_id, pdf = prepare_indexed_project(client, workdir, "P3 EndToEnd")

        answered = client.post(
            "/api/literature/answer",
            json={
                "project_path": str(project),
                "question": f"What does the paper say about the {PAGE_TWO_MARKER}?",
                "source_ids": [source_id],
                "top_k": 5,
            },
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()

        assert body["status"] == "answered"
        assert body["insufficient_reason"] is None
        assert body["source_ids"] == [source_id]
        assert body["project_root"] == str(project)
        assert body["model_provider"] == "scripted"
        assert body["model_name"] == "scripted-answer-v1"
        assert len(body["model_config_hash"]) == 64
        assert answer_provider.prompts, "the answer model was never called"

        evidence_ids = {item["span"]["evidence_id"] for item in body["evidence"]}
        assert evidence_ids
        for claim in body["claims"]:
            assert claim["evidence_ids"]
            assert set(claim["evidence_ids"]) <= evidence_ids

        pages = parsed_pages(pdf)
        for item in body["evidence"]:
            span = item["span"]
            assert item["source_id"] == source_id
            assert span["coordinate_space"] == "normalized_page_text_v1"
            assert span["page_start"] == span["page_end"]
            assert (
                pages[span["page_start"]][span["char_start"] : span["char_end"]]
                == (span["exact_quote"])
            )
        assert any(PAGE_TWO_MARKER in item["span"]["exact_quote"] for item in body["evidence"])

        # The same question asked with no scope must be refused outright.
        unscoped = client.post(
            "/api/literature/answer",
            json={"project_path": str(project), "question": "Anything?", "source_ids": []},
        )
        assert unscoped.status_code == 400
        assert unscoped.json()["detail"]["code"] == "scope_required"

        # A question the corpus cannot answer yields an explicit insufficiency and
        # never a fabricated citation.
        unanswerable = client.post(
            "/api/literature/answer",
            json={
                "project_path": str(project),
                "question": f"{UNANSWERABLE_MARKER}: what is the airspeed of a swallow?",
                "source_ids": [source_id],
                "top_k": 5,
            },
        )
        assert unanswerable.status_code == 200, unanswerable.text
        unanswered = unanswerable.json()
        assert unanswered["status"] == "insufficient"
        assert unanswered["insufficient_reason"] == "model_reported_insufficient"
        assert unanswered["claims"] == []
        assert unanswered["evidence"] == []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
