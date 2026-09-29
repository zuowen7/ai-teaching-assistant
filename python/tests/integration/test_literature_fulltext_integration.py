"""M5 integration test: acquisition against the real project store and real index.

Scope (plan section 5.8): the acquired PDF must land inside the project's
``references/`` directory, the manifest must record provenance (path, sha256,
size, MIME, status), and the P2B page index plus evidence resolution must work on
exactly that file — all through the real routers and the real ChromaDB store,
with only the downloader replaced so the test stays offline.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from routers.literature import (  # noqa: E402
    ProjectSourceManifestStore,
    RagPageIndexStore,
    register_literature_routes,
)
from routers.project import register_project  # noqa: E402
from routers.rag import register_rag_routes  # noqa: E402
from src.literature.evidence import normalize_page_text  # noqa: E402
from src.literature.fulltext import DownloadedArtifact  # noqa: E402
from src.literature.models import (  # noqa: E402
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    PaperRecord,
    SearchQuery,
)
from src.literature.providers.fixture import FixtureProvider  # noqa: E402
from src.literature.service import LiteratureService  # noqa: E402

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)
QUERY = 'all:"retrieval augmented generation"'
RESEARCH_QUESTION = "How does retrieval augmented generation ground answers?"
PDF_URL = "https://arxiv.org/pdf/cs/9901001v2"
PAGE_ONE = (
    "Acquired page one explains how retrieved passages ground an answer.\n\n"
    "It states the problem the study addresses."
)
PAGE_TWO = (
    "Acquired page two reports the evaluation protocol and its metrics.\n\n"
    "Only this page names the held-out evaluation split."
)
PAGE_TWO_MARKER = "held-out evaluation split"


def write_pdf(path: Path, page_texts: list[str]) -> Path:
    import fitz

    document = fitz.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_text((72, 96), text, fontsize=11)
    document.save(str(path))
    document.close()
    return path


def make_record() -> PaperRecord:
    return PaperRecord(
        provider="fixture",
        provider_record_id="cs/9901001",
        external_ids=ExternalIdentifiers(doi="10.1000/acquire", arxiv="cs/9901001v2"),
        title="Acquired Full Text Paper",
        authors=["Ada Researcher"],
        year=2026,
        venue="PoC Proceedings",
        abstract="A deterministic record for acquisition verification.",
        categories=["cs.AI"],
        record_url="https://arxiv.org/abs/cs/9901001v2",
        access_locations=[
            AccessLocation(
                kind=AccessKind.PDF,
                url=PDF_URL,
                access_status=AccessStatus.OPEN,
                mime_type="application/pdf",
                is_primary=True,
            )
        ],
        source_query=QUERY,
        retrieved_at=NOW,
    )


class StubDownloader:
    """Offline downloader: returns the bytes of a locally generated PDF."""

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


@pytest.fixture()
def ctx():
    pytest.importorskip("chromadb")
    runtime_dir = Path(tempfile.mkdtemp(prefix="m5-integration-"))
    workdir = Path(tempfile.mkdtemp(prefix="m5-project-"))
    pdf = write_pdf(workdir / "source.pdf", [PAGE_ONE, PAGE_TWO])
    downloader = StubDownloader(pdf)

    app = FastAPI()
    register_project(
        app,
        cloud_only=False,
        load_config=lambda: {"translator": {}, "agent": {}},
        runtime_dir=runtime_dir,
        data_root=runtime_dir / "data",
    )
    state_rag = register_rag_routes(app, runtime_dir=runtime_dir)
    record = make_record()
    provider = FixtureProvider(
        fixture_name="m5-fixture-v1",
        snapshot_at=NOW,
        records=[record],
        search_results={QUERY: [record.paper_id]},
    )
    register_literature_routes(
        app,
        service=LiteratureService(
            providers=[provider],
            project_store=ProjectSourceManifestStore(),
            index_store=RagPageIndexStore(state_rag),
            downloader=downloader,
            now_factory=lambda: NOW,
        ),
    )

    location = workdir / "projects"
    location.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        yield client, location, downloader, pdf, record

    shutil.rmtree(runtime_dir, ignore_errors=True)
    shutil.rmtree(workdir, ignore_errors=True)


def create_and_import(client: TestClient, location: Path, record: PaperRecord) -> tuple[Path, str]:
    created = client.post(
        "/api/project/create",
        json={
            "name": "M5 Acquisition",
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
            "query": SearchQuery(query=QUERY).model_dump(mode="json"),
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
    imported = client.post(
        "/api/literature/import",
        json={
            "project_path": str(project),
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record.paper_id],
        },
    )
    assert imported.status_code == 200, imported.text
    return project, imported.json()["results"][0]["source_id"]


def test_acquisition_writes_into_the_project_and_feeds_the_evidence_index(ctx) -> None:
    client, location, downloader, pdf, record = ctx
    project, source_id = create_and_import(client, location, record)

    acquired = client.post(
        "/api/literature/fulltext",
        json={"project_path": str(project), "source_id": source_id},
    )

    assert acquired.status_code == 200, acquired.text
    body = acquired.json()
    assert body["status"] == "fulltext_ready"
    assert downloader.urls == [PDF_URL]
    assert body["source_url"] == PDF_URL
    assert body["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()

    local_path = Path(body["local_path"])
    assert local_path.is_file()
    assert local_path.parent == (project / "references").resolve()
    assert local_path.read_bytes() == pdf.read_bytes()

    sources = client.get("/api/project/sources", params={"project_path": str(project)}).json()
    source = sources["sources"][0]
    assert Path(source["original_path"]) == local_path
    fulltext = source["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "fulltext_ready"
    assert fulltext["sha256"] == body["sha256"]
    assert fulltext["file_size_bytes"] == len(pdf.read_bytes())
    assert fulltext["mime_type"] == "application/pdf"
    assert fulltext["source_url"] == PDF_URL

    repeated = client.post(
        "/api/literature/fulltext",
        json={"project_path": str(project), "source_id": source_id},
    )
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["code"] == "fulltext_already_present"

    indexed = client.post(
        "/api/literature/index",
        json={"project_path": str(project), "source_id": source_id},
    )
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["artifact_sha256"] == body["sha256"]
    assert indexed.json()["page_count"] == 2

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
    assert hits
    page_two_hit = next(hit for hit in hits if hit["metadata"]["page_start"] == 2)

    resolved = client.post(
        "/api/literature/evidence",
        json={
            "project_path": str(project),
            "source_id": source_id,
            "chunk_id": page_two_hit["chunk_id"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    span = resolved.json()["span"]
    assert span["page_start"] == span["page_end"] == 2
    assert span["artifact_sha256"] == body["sha256"]
    assert PAGE_TWO_MARKER in span["exact_quote"]

    from src.parser import extract_document

    pages = {
        page.page_num: normalize_page_text(page.text)
        for page in extract_document(local_path).pages
        if normalize_page_text(page.text)
    }
    assert pages[2][span["char_start"] : span["char_end"]] == span["exact_quote"]
