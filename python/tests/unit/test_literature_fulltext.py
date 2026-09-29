"""Unit tests for M5 open-PDF acquisition (spec: plan section 5.8, D-023).

Written before the implementation on purpose: every assertion here encodes a
frozen rule (only open https PDF locations, size/timeout/content-type/magic-byte
checks, explicit failure states, no silent overwrite of an existing artifact).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.literature.fulltext import (
    MAX_PDF_BYTES,
    DownloadedArtifact,
    FullTextDownloadError,
    FullTextErrorCode,
    HttpFullTextDownloader,
    select_open_pdf_location,
)
from src.literature.models import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    ExternalIdentifiers,
    FullTextStatus,
    PaperRecord,
    SearchGenerationMethod,
    SearchPlanDraft,
    SearchQuery,
)
from src.literature.service import (
    LiteratureService,
    LiteratureServiceError,
    LiteratureServiceErrorCode,
)
from tests.unit.test_literature_service import (
    NOW,
    MemoryProjectStore,
    StaticProvider,
    make_record,
    make_service,
    search,
)

PROJECT = "D:/projects/m5"
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PDF_URL = "https://arxiv.org/pdf/cs/9901001v2"


def open_pdf_location(url: str = PDF_URL, **overrides: Any) -> AccessLocation:
    payload: dict[str, Any] = {
        "kind": AccessKind.PDF,
        "url": url,
        "access_status": AccessStatus.OPEN,
        "mime_type": "application/pdf",
        "is_primary": True,
    }
    payload.update(overrides)
    return AccessLocation.model_validate(payload)


def downloadable_record(**overrides: Any) -> PaperRecord:
    record = make_record("alpha", "cs/9901001")
    payload = record.model_dump(mode="json")
    payload["access_locations"] = [
        open_pdf_location().model_dump(mode="json"),
    ]
    payload.update(overrides)
    # Access locations are part of the metadata snapshot, so it must be
    # recomputed after overriding them.
    payload["metadata_snapshot_hash"] = ""
    return PaperRecord.model_validate(payload)


def make_downloader(
    *,
    body: bytes = PDF_BYTES,
    status_code: int = 200,
    content_type: str = "application/pdf",
    exc: Exception | None = None,
) -> HttpFullTextDownloader:
    def handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc
        if request.url.path.endswith("/start"):
            return httpx.Response(302, headers={"location": "https://example.test/end"})
        return httpx.Response(status_code, content=body, headers={"content-type": content_type})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpFullTextDownloader(client=client)


class TestSelectOpenPdfLocation:
    def test_prefers_the_primary_open_pdf(self) -> None:
        primary = open_pdf_location("https://arxiv.org/pdf/primary.pdf")
        other = open_pdf_location("https://arxiv.org/pdf/other.pdf", is_primary=False)
        record = downloadable_record(
            access_locations=[
                other.model_dump(mode="json"),
                primary.model_dump(mode="json"),
            ]
        )

        assert select_open_pdf_location(record) == primary

    @pytest.mark.parametrize(
        "location",
        [
            open_pdf_location(access_status=AccessStatus.RESTRICTED),
            open_pdf_location(access_status=AccessStatus.UNKNOWN),
            open_pdf_location(kind=AccessKind.LANDING_PAGE),
            open_pdf_location(kind=AccessKind.HTML),
            open_pdf_location("http://arxiv.org/pdf/insecure.pdf"),
        ],
    )
    def test_rejects_locations_that_are_not_open_https_pdfs(self, location: AccessLocation) -> None:
        record = downloadable_record(access_locations=[location.model_dump(mode="json")])

        assert select_open_pdf_location(record) is None

    def test_returns_none_without_access_locations(self) -> None:
        assert (
            select_open_pdf_location(make_record("alpha", "cs/9901001", with_access=False)) is None
        )


class TestHttpFullTextDownloader:
    async def test_downloads_and_hashes_a_pdf(self) -> None:
        downloader = make_downloader()

        artifact = await downloader.download(PDF_URL)

        assert isinstance(artifact, DownloadedArtifact)
        assert artifact.content == PDF_BYTES
        assert artifact.size_bytes == len(PDF_BYTES)
        assert artifact.mime_type == "application/pdf"
        assert artifact.source_url == PDF_URL
        assert len(artifact.sha256) == 64
        await downloader.aclose()

    async def test_accepts_the_octet_stream_content_type_for_a_pdf_body(self) -> None:
        downloader = make_downloader(content_type="application/octet-stream")

        artifact = await downloader.download(PDF_URL)

        assert artifact.content == PDF_BYTES
        await downloader.aclose()

    @pytest.mark.parametrize(
        ("url", "code"),
        [
            ("http://arxiv.org/pdf/x.pdf", FullTextErrorCode.INVALID_URL),
            ("ftp://arxiv.org/pdf/x.pdf", FullTextErrorCode.INVALID_URL),
            ("https:///no-host.pdf", FullTextErrorCode.INVALID_URL),
        ],
    )
    async def test_rejects_non_https_urls(self, url: str, code: FullTextErrorCode) -> None:
        downloader = make_downloader()

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(url)

        assert captured.value.code is code
        await downloader.aclose()

    @pytest.mark.parametrize("status_code", [403, 404, 500, 503])
    async def test_http_errors_are_not_treated_as_content(self, status_code: int) -> None:
        downloader = make_downloader(status_code=status_code, content_type="text/html")

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.DOWNLOAD_FAILED
        assert str(status_code) in str(captured.value)
        await downloader.aclose()

    async def test_timeouts_are_reported_as_download_failures(self) -> None:
        downloader = make_downloader(exc=httpx.ReadTimeout("too slow"))

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.DOWNLOAD_FAILED
        await downloader.aclose()

    async def test_transport_errors_are_reported_as_download_failures(self) -> None:
        downloader = make_downloader(exc=httpx.ConnectError("no route"))

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.DOWNLOAD_FAILED
        await downloader.aclose()

    async def test_oversized_bodies_are_rejected(self) -> None:
        downloader = make_downloader(body=b"%PDF-" + b"0" * (MAX_PDF_BYTES + 1))

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.TOO_LARGE
        await downloader.aclose()

    @pytest.mark.parametrize(
        "content_type",
        ["text/html", "application/json", "text/plain"],
    )
    async def test_unexpected_content_types_are_rejected(self, content_type: str) -> None:
        downloader = make_downloader(content_type=content_type)

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.UNEXPECTED_CONTENT_TYPE
        await downloader.aclose()

    async def test_html_error_page_disguised_as_pdf_is_rejected(self) -> None:
        downloader = make_downloader(body=b"<html><body>Not found</body></html>")

        with pytest.raises(FullTextDownloadError) as captured:
            await downloader.download(PDF_URL)

        assert captured.value.code is FullTextErrorCode.NOT_A_PDF
        await downloader.aclose()

    async def test_follows_a_redirect_to_the_pdf(self) -> None:
        downloader = make_downloader()

        artifact = await downloader.download("https://example.test/start")

        assert artifact.content == PDF_BYTES
        await downloader.aclose()


class MemoryArtifactStore(MemoryProjectStore):
    """Project store that can persist an acquired artifact into a real directory."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.references = root / "references"
        self.references.mkdir(parents=True, exist_ok=True)

    def store_source_artifact(
        self,
        project_path: str,
        source_id: str,
        filename: str,
        content: bytes,
    ) -> str:
        safe_name = Path(filename).name
        target = self.references / safe_name
        index = 1
        while target.exists():
            target = self.references / f"{Path(safe_name).stem}-{index}{Path(safe_name).suffix}"
            index += 1
        target.write_bytes(content)
        self.artifacts[source_id] = str(target)
        return str(target)


class StubDownloader:
    def __init__(self, artifact: DownloadedArtifact | None = None, error: Exception | None = None):
        self.artifact = artifact
        self.error = error
        self.urls: list[str] = []

    async def download(self, url: str) -> DownloadedArtifact:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        assert self.artifact is not None
        return self.artifact

    async def aclose(self) -> None:
        return None


def make_artifact(**overrides: Any) -> DownloadedArtifact:
    content = overrides.pop("content", PDF_BYTES)
    payload = {
        "content": content,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "mime_type": "application/pdf",
        "source_url": PDF_URL,
    }
    payload.update(overrides)
    return DownloadedArtifact(**payload)


async def make_acquirable_source(tmp_path: Path, *, with_location: bool = True):
    store = MemoryArtifactStore(tmp_path)
    record = (
        downloadable_record()
        if with_location
        else make_record("alpha", "cs/9901001", with_access=False)
    )
    provider = StaticProvider("alpha", [record])
    service = LiteratureService(
        providers=[provider],
        project_store=store,
        now_factory=lambda: NOW,
        source_id_factory=lambda: "src_lit_m5000000000001",
    )
    execution = await search(service, "alpha")
    batch = await service.import_selection(
        project_path=PROJECT,
        search_execution_id=execution.search_execution_id,
        paper_ids=[record.paper_id],
    )
    return service, store, batch.results[0].source_id


async def test_acquire_uses_the_open_location_and_records_provenance(tmp_path: Path) -> None:
    service, store, source_id = await make_acquirable_source(tmp_path)
    downloader = StubDownloader(make_artifact())
    service._downloader = downloader

    result = await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert downloader.urls == [PDF_URL]
    assert result.status is FullTextStatus.FULLTEXT_READY
    assert result.source_url == PDF_URL
    assert result.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    literature = store.read_sources(PROJECT)[0]["metadata"]["literature"]
    fulltext = literature["fulltext"]
    assert fulltext["status"] == "fulltext_ready"
    assert fulltext["local_path"]
    assert Path(fulltext["local_path"]).is_file()
    assert Path(fulltext["local_path"]).read_bytes() == PDF_BYTES
    assert fulltext["source_url"] == PDF_URL
    assert fulltext["file_size_bytes"] == len(PDF_BYTES)
    assert fulltext["mime_type"] == "application/pdf"


async def test_acquire_records_the_acquiring_state_before_finishing(tmp_path: Path) -> None:
    service, store, source_id = await make_acquirable_source(tmp_path)
    observed: list[str] = []

    class ObservingDownloader(StubDownloader):
        async def download(self, url: str) -> DownloadedArtifact:
            observed.append(
                store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]["status"]
            )
            return await super().download(url)

    service._downloader = ObservingDownloader(make_artifact())

    await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert observed == ["acquiring"]


async def test_source_without_open_location_is_access_unavailable(tmp_path: Path) -> None:
    service, store, source_id = await make_acquirable_source(tmp_path, with_location=False)
    downloader = StubDownloader(make_artifact())
    service._downloader = downloader

    with pytest.raises(LiteratureServiceError) as captured:
        await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.ACCESS_UNAVAILABLE
    assert downloader.urls == []
    fulltext = store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "access_unavailable"
    assert fulltext["failure_reason"]
    assert fulltext["sha256"] is None


async def test_download_failure_is_acquire_failed_without_writing_a_file(tmp_path: Path) -> None:
    service, store, source_id = await make_acquirable_source(tmp_path)
    error = FullTextDownloadError(FullTextErrorCode.NOT_A_PDF, "response is not a PDF")
    service._downloader = StubDownloader(error=error)

    with pytest.raises(LiteratureServiceError) as captured:
        await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.ACQUIRE_FAILED
    assert captured.value.details["code"] == "not_a_pdf"
    fulltext = store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "acquire_failed"
    assert fulltext["failure_reason"]
    assert list(store.references.iterdir()) == []


async def test_existing_artifact_is_not_overwritten_without_force(tmp_path: Path) -> None:
    service, store, source_id = await make_acquirable_source(tmp_path)
    service._downloader = StubDownloader(make_artifact())
    first = await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)
    first_path = Path(
        store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]["local_path"]
    )

    with pytest.raises(LiteratureServiceError) as captured:
        await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.FULLTEXT_ALREADY_PRESENT
    assert first_path.read_bytes() == PDF_BYTES

    forced = await service.acquire_fulltext(
        project_path=PROJECT,
        source_id=source_id,
        force=True,
    )
    assert forced.status is FullTextStatus.FULLTEXT_READY
    assert forced.sha256 == first.sha256


async def test_a_downloader_that_misreports_its_hash_is_refused(tmp_path: Path) -> None:
    """D-023 rule 4: the recorded hash is computed from the bytes, not trusted."""

    service, store, source_id = await make_acquirable_source(tmp_path)
    service._downloader = StubDownloader(make_artifact(sha256="f" * 64))

    with pytest.raises(LiteratureServiceError) as captured:
        await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)

    assert captured.value.code is LiteratureServiceErrorCode.ACQUIRE_FAILED
    assert captured.value.details["code"] == "downloader_hash_mismatch"
    fulltext = store.read_sources(PROJECT)[0]["metadata"]["literature"]["fulltext"]
    assert fulltext["status"] == "acquire_failed"
    assert list(store.references.iterdir()) == []


async def test_indexing_still_works_on_an_acquired_artifact(tmp_path: Path) -> None:
    from tests.unit.test_literature_indexing import MemoryPageIndexStore, write_pdf

    service, store, source_id = await make_acquirable_source(tmp_path)
    pdf = write_pdf(tmp_path / "acquired.pdf", ["Acquired page one.", "Acquired page two."])
    service._downloader = StubDownloader(make_artifact(content=pdf.read_bytes()))
    index_store = MemoryPageIndexStore()
    service._index_store = index_store

    acquired = await service.acquire_fulltext(project_path=PROJECT, source_id=source_id)
    indexed = await service.index_source(project_path=PROJECT, source_id=source_id)

    assert indexed.status is FullTextStatus.INDEXED
    assert indexed.artifact_sha256 == acquired.sha256
    assert index_store.documents[f"project:{source_id}"]["chunk_count"] == indexed.chunk_count
