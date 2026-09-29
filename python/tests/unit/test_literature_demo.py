"""P4 unit tests for the fixed demo corpus and the demo run record.

Scope (frozen in docs/literature-research-poc-plan.md section 5.10): the cached
corpus is a *demo and regression* asset.  It must be loadable offline, it must
never look like a live result, a missing or corrupt corpus must fail explicitly
instead of silently degrading, and a run record must be comparable across repeat
runs while never carrying a secret.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.literature.demo_corpus import (
    DemoCorpusError,
    build_fixture_provider,
    load_demo_corpus,
    materialize_pdf,
    resolve_corpus_path,
)
from src.literature.demo_run import (
    DemoRun,
    DemoRunRecorder,
    StepStatus,
    run_record_filename,
    write_run_record,
)
from src.literature.models import SearchQuery, SearchResultMode

pytest.importorskip("fitz")

REPO_CORPUS = resolve_corpus_path()
NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)


def flatten(text: str) -> str:
    """Compare page text without depending on where the demo PDF wraps lines."""

    return " ".join(text.split())


def minimal_corpus(**overrides) -> dict:
    payload = {
        "version": 1,
        "corpus_id": "unit-demo-v1",
        "kind": "synthetic-demo-corpus",
        "provenance": "unit test corpus",
        "demo": {
            "confirmed_query": 'all:"unit query"',
            "question": "What does the corpus report?",
        },
        "records": [
            {
                "provider_record_id": "demo-1001",
                "title": "Unit Demo Paper",
                "authors": ["Unit Author"],
                "year": 2026,
                "venue": "Unit Venue",
                "abstract": "unit abstract",
                "categories": ["cs.CL"],
                "access": "open",
                "pages": ["Unit page one text.", "Unit page two text."],
            }
        ],
        "search_results": {'all:"unit query"': ["demo-1001"]},
    }
    payload.update(overrides)
    return payload


def write_corpus(tmp_path: Path, payload: dict, name: str = "corpus.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestDemoCorpusLoading:
    def test_repository_corpus_loads_with_stable_order(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)

        assert corpus.corpus_id == "literature-poc-demo-v1"
        assert corpus.kind == "synthetic-demo-corpus"
        assert "合成" in corpus.provenance
        assert [record.provider_record_id for record in corpus.records] == [
            "demo-0001",
            "demo-0002",
            "demo-0003",
        ]
        assert corpus.confirmed_query == 'all:"evidence traceable question answering"'
        assert corpus.question

    def test_every_record_keeps_its_pages_and_declares_no_open_location(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)

        for record in corpus.records:
            pages = corpus.pages_for(record.paper_id)
            assert pages, record.provider_record_id
            assert [number for number, _ in pages] == list(range(1, len(pages) + 1))
            assert all(text.strip() for _, text in pages)
            # The demo corpus never pretends to offer a downloadable full text.
            assert record.access_locations == []
            assert corpus.access_locations[record.paper_id] == ()

    def test_corpus_declares_which_records_the_demo_indexes(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)

        indexed = [record.provider_record_id for record in corpus.records_to_index()]
        assert indexed == ["demo-0001", "demo-0002"]
        assert "demo-0003" in {record.provider_record_id for record in corpus.records}
        assert corpus.record_for_provider_id("demo-0001") is not None
        assert corpus.record_for_provider_id("demo-9999") is None

    def test_corpus_identifiers_cannot_be_mistaken_for_real_papers(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)

        for record in corpus.records:
            assert record.provider_record_id.startswith("demo-")
            assert record.external_ids.arxiv is None
            assert record.external_ids.doi is None

    def test_missing_corpus_fails_explicitly(self, tmp_path: Path) -> None:
        with pytest.raises(DemoCorpusError) as excinfo:
            load_demo_corpus(tmp_path / "absent.json")
        assert excinfo.value.code == "corpus_missing"

    def test_corrupt_corpus_fails_explicitly(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(DemoCorpusError) as excinfo:
            load_demo_corpus(path)
        assert excinfo.value.code == "corpus_invalid"

    @pytest.mark.parametrize(
        "override",
        [
            {"version": 99},
            {"kind": "live-cache"},
            {"records": []},
            {"demo": {}},
            {"search_results": {}},
            {"search_results": {'all:"unit query"': ["demo-9999"]}},
        ],
    )
    def test_structurally_wrong_corpus_is_refused(self, tmp_path: Path, override: dict) -> None:
        path = write_corpus(tmp_path, minimal_corpus(**override))
        with pytest.raises(DemoCorpusError) as excinfo:
            load_demo_corpus(path)
        assert excinfo.value.code == "corpus_invalid"

    def test_record_without_pages_is_refused(self, tmp_path: Path) -> None:
        payload = minimal_corpus()
        payload["records"][0]["pages"] = []
        path = write_corpus(tmp_path, payload)
        with pytest.raises(DemoCorpusError):
            load_demo_corpus(path)


class TestFixtureProviderWiring:
    async def test_fixture_provider_never_reports_a_live_result(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)
        provider = build_fixture_provider(corpus)

        assert provider.name == "fixture"
        capabilities = provider.capabilities()
        assert capabilities.result_mode is SearchResultMode.FIXTURE
        assert capabilities.supports_fulltext_download is False

        page = await provider.search(SearchQuery(query=corpus.confirmed_query))
        assert page.result_mode is SearchResultMode.FIXTURE
        assert page.provenance_label == corpus.corpus_id
        assert [record.provider_record_id for record in page.records] == [
            "demo-0001",
            "demo-0002",
            "demo-0003",
        ]

    async def test_no_record_offers_a_downloadable_full_text(self) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)
        provider = build_fixture_provider(corpus)

        for record in corpus.records:
            assert await provider.resolve_access(record) == []


class TestMaterializePdf:
    def test_materialized_pdf_reparses_to_the_corpus_pages(self, tmp_path: Path) -> None:
        from src.parser import extract_document

        corpus = load_demo_corpus(REPO_CORPUS)
        record = corpus.records[0]
        target = materialize_pdf(corpus, record.paper_id, tmp_path / "demo.pdf")

        assert target.is_file()
        document = extract_document(target)
        parsed = {
            page.page_num: flatten(page.text) for page in document.pages if flatten(page.text)
        }
        expected = {number: flatten(text) for number, text in corpus.pages_for(record.paper_id)}
        assert parsed == expected

    def test_unknown_paper_is_refused(self, tmp_path: Path) -> None:
        corpus = load_demo_corpus(REPO_CORPUS)
        with pytest.raises(DemoCorpusError) as excinfo:
            materialize_pdf(corpus, "paper_missing", tmp_path / "x.pdf")
        assert excinfo.value.code == "unknown_paper"


class _StubResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StubClient:
    """Minimal HTTP client for driving run_demo without a server."""

    def __init__(self, responses: dict[str, _StubResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def post(self, url: str, json=None, data=None, files=None):
        self.calls.append(url)
        for suffix, response in self._responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected request: {url}")


def _search_page(provider: str = "arxiv") -> _StubResponse:
    return _StubResponse(
        {
            "search_execution_id": "search_exec_" + "c" * 32,
            "page": {
                "provider": provider,
                "result_mode": "live" if provider == "arxiv" else "fixture",
                "records": [
                    {"paper_id": "paper_demo_0001", "title": "Demo Paper A", "access": "open"}
                ],
                "total_results": 1,
            },
        }
    )


class TestDemoRunFailureScenarios:
    """Plan 5.10: each failure must be recorded with its own reason."""

    def test_provider_failure_stops_the_run_with_its_code(self, tmp_path: Path) -> None:
        from scripts.literature_demo import run_demo

        client = _StubClient(
            {
                "/api/literature/search": _StubResponse(
                    {"detail": {"code": "rate_limited", "message": "slow down"}}, status_code=429
                )
            }
        )
        run = run_demo(
            client,
            project_path=str(tmp_path),
            provider="arxiv",
            corpus=load_demo_corpus(REPO_CORPUS),
            workspace=tmp_path / "workspace",
        )

        assert [step.name for step in run.steps] == ["search"]
        assert run.steps[0].status is StepStatus.FAILED
        assert run.steps[0].reason == "rate_limited"
        assert run.totals == {"step": 1, "ok": 0, "failed": 1, "skipped": 0}
        assert run.answer_status is None

    def test_index_failure_skips_the_dependent_answer_step(self, tmp_path: Path) -> None:
        from scripts.literature_demo import run_demo

        client = _StubClient(
            {
                "/api/literature/search": _search_page(),
                "/api/literature/import": _StubResponse(
                    {
                        "created_count": 1,
                        "reused_count": 0,
                        "results": [{"paper_id": "paper_demo_0001", "source_id": "src_demo_0001"}],
                    }
                ),
                "/api/literature/fulltext": _StubResponse(
                    {
                        "source_id": "src_demo_0001",
                        "status": "fulltext_ready",
                        "local_path": "x.pdf",
                    }
                ),
                "/api/literature/index": _StubResponse(
                    {"detail": {"code": "source_artifact_missing", "message": "no pdf"}},
                    status_code=409,
                ),
            }
        )
        run = run_demo(
            client,
            project_path=str(tmp_path),
            provider="arxiv",
            corpus=load_demo_corpus(REPO_CORPUS),
            workspace=tmp_path / "workspace",
        )

        steps = {step.name: step for step in run.steps}
        assert steps["index"].status is StepStatus.FAILED
        assert steps["index"].reason == "source_artifact_missing"
        assert steps["answer"].status is StepStatus.SKIPPED
        assert steps["answer"].reason == "dependency_failed"
        assert steps["resolve_evidence"].status is StepStatus.SKIPPED
        assert steps["resolve_evidence"].reason == "no_evidence_to_resolve"
        assert run.totals["failed"] == 1
        # attach (live mode), answer and the dependent evidence step are skipped.
        assert run.totals["skipped"] == 3
        assert steps["attach_fulltext"].reason == "user_attachment_required"

    def test_missing_full_text_leaves_nothing_to_index(self, tmp_path: Path) -> None:
        from scripts.literature_demo import run_demo

        client = _StubClient(
            {
                "/api/literature/search": _search_page(),
                "/api/literature/import": _StubResponse(
                    {
                        "created_count": 1,
                        "reused_count": 0,
                        "results": [{"paper_id": "paper_demo_0001", "source_id": "src_demo_0001"}],
                    }
                ),
                "/api/literature/fulltext": _StubResponse(
                    {"detail": {"code": "access_unavailable", "message": "no open pdf"}},
                    status_code=409,
                ),
            }
        )
        run = run_demo(
            client,
            project_path=str(tmp_path),
            provider="arxiv",
            corpus=load_demo_corpus(REPO_CORPUS),
            workspace=tmp_path / "workspace",
        )

        steps = {step.name: step for step in run.steps}
        assert steps["acquire_fulltext"].status is StepStatus.FAILED
        assert steps["acquire_fulltext"].reason == "access_unavailable"
        assert steps["attach_fulltext"].status is StepStatus.SKIPPED
        assert steps["attach_fulltext"].reason == "user_attachment_required"
        assert steps["index"].reason == "no_indexed_source"
        assert steps["answer"].reason == "dependency_failed"


class TestDemoRunRecorder:
    def test_steps_keep_order_and_totals_aggregate(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture",
            provider="fixture",
            confirmed_query='all:"q"',
            question="Q?",
            started_at=NOW,
        )
        recorder.record(name="search", status=StepStatus.OK, duration_ms=12, counts={"records": 3})
        recorder.record(
            name="acquire_fulltext",
            status=StepStatus.FAILED,
            duration_ms=5,
            reason="access_unavailable",
        )
        recorder.record(name="answer", status=StepStatus.SKIPPED, reason="dependency_failed")
        run = recorder.finish(answer_status="insufficient", finished_at=NOW + timedelta(seconds=2))

        assert [step.name for step in run.steps] == ["search", "acquire_fulltext", "answer"]
        assert run.steps[0].counts == {"records": 3}
        assert run.steps[1].reason == "access_unavailable"
        assert run.totals == {"step": 3, "ok": 1, "failed": 1, "skipped": 1}
        assert run.answer_status == "insufficient"
        assert run.duration_ms == 2000
        assert run.mode == "fixture"

    def test_run_id_is_stable_for_the_same_outcome_only(self) -> None:
        def build(status: StepStatus):
            recorder = DemoRunRecorder(
                mode="fixture",
                provider="fixture",
                confirmed_query='all:"q"',
                question="Q?",
                started_at=NOW,
            )
            recorder.record(
                name="search",
                status=status,
                reason=None if status is StepStatus.OK else "provider_unavailable",
                counts={"records": 3},
            )
            return recorder.finish(finished_at=NOW + timedelta(seconds=1))

        first = build(StepStatus.OK)
        repeat = build(StepStatus.OK)
        failed = build(StepStatus.FAILED)

        assert first.run_id == repeat.run_id
        assert first.run_id.startswith("run_")
        assert first.run_id != failed.run_id

    def test_recording_is_rejected_when_it_carries_a_secret(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
        )
        with pytest.raises(ValueError):
            recorder.record(
                name="answer",
                status=StepStatus.OK,
                counts={"api_key_count": 1},
            )
        with pytest.raises(ValueError):
            recorder.record(
                name="answer",
                status=StepStatus.OK,
                detail={"authorization": "Bearer x"},
            )

    def test_serialized_record_has_no_secret_like_keys(self) -> None:
        recorder = DemoRunRecorder(
            mode="live",
            provider="arxiv",
            confirmed_query='all:"q"',
            question="Q?",
            started_at=NOW,
        )
        recorder.record(name="search", status=StepStatus.OK, counts={"records": 1})
        run = recorder.finish(answer_status="answered", finished_at=NOW + timedelta(seconds=1))

        payload = json.loads(run.model_dump_json())
        flattened = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("api_key", "apikey", "authorization", "password", "secret"):
            assert forbidden not in flattened

    def test_step_requires_a_reason_when_it_does_not_succeed(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
        )
        with pytest.raises(ValueError):
            recorder.record(name="search", status=StepStatus.FAILED)

    def test_recording_is_rejected_when_a_secret_hides_in_a_value(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
        )
        with pytest.raises(ValueError):
            recorder.record(
                name="answer",
                status=StepStatus.OK,
                detail={"note": "sk-abcdef0123456789"},
            )

    def test_answer_model_config_hash_is_recorded_when_available(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
        )
        recorder.record(name="answer", status=StepStatus.OK)
        run = recorder.finish(
            answer_status="answered",
            answer_model_config_hash="a" * 64,
            finished_at=NOW + timedelta(seconds=1),
        )

        assert run.answer_model_config_hash == "a" * 64
        assert "sk-" not in json.dumps(json.loads(run.model_dump_json()))

    def test_same_second_repeats_do_not_overwrite_each_other(self, tmp_path: Path) -> None:
        def build() -> DemoRun:
            recorder = DemoRunRecorder(
                mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
            )
            recorder.record(name="search", status=StepStatus.OK)
            return recorder.finish(finished_at=NOW + timedelta(seconds=1))

        first = write_run_record(build(), tmp_path)
        second = write_run_record(build(), tmp_path)

        assert first != second
        assert len(list(tmp_path.glob("*.json"))) == 2

    def test_record_filename_is_unique_per_run(self) -> None:
        recorder = DemoRunRecorder(
            mode="fixture", provider="fixture", confirmed_query='all:"q"', started_at=NOW
        )
        recorder.record(name="search", status=StepStatus.OK)
        first = recorder.finish(finished_at=NOW + timedelta(seconds=1))
        recorder2 = DemoRunRecorder(
            mode="fixture",
            provider="fixture",
            confirmed_query='all:"q"',
            started_at=NOW + timedelta(hours=1),
        )
        recorder2.record(name="search", status=StepStatus.OK)
        second = recorder2.finish(finished_at=NOW + timedelta(hours=1, seconds=1))

        assert run_record_filename(first) != run_record_filename(second)
        assert run_record_filename(first).endswith(".json")
        assert first.run_id in run_record_filename(first)
