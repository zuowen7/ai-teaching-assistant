"""Unit tests for the normalized literature-domain contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.literature import (
    AccessKind,
    AccessLocation,
    AccessStatus,
    AnswerClaim,
    EvidenceSpan,
    EvidenceStatus,
    ExternalIdentifiers,
    FullTextArtifact,
    FullTextStatus,
    PaperRecord,
    SearchGenerationMethod,
    SearchPage,
    SearchPlan,
    SearchPlanDraft,
    SearchQuery,
    SearchResultMode,
)

SNAPSHOT_AT = datetime(2026, 9, 20, 1, 2, 3, tzinfo=UTC)


def make_record(**overrides) -> PaperRecord:
    payload = {
        "provider": " arXiv ",
        "provider_record_id": "2401.12345",
        "external_ids": {
            "doi": "https://doi.org/10.1000/Example",
            "arxiv": "https://arxiv.org/abs/2401.12345v2",
        },
        "title": "  Multi-Agent   Academic Writing  ",
        "authors": [" Alice Example ", "Bob Example", "alice example"],
        "year": 2024,
        "venue": " arXiv ",
        "abstract": " A   reproducible fixture abstract. ",
        "categories": ["cs.AI", " cs.CL ", "CS.ai"],
        "record_url": "https://arxiv.org/abs/2401.12345",
        "access_locations": [
            AccessLocation(
                kind=AccessKind.PDF,
                url="https://arxiv.org/pdf/2401.12345",
                access_status=AccessStatus.OPEN,
                mime_type="application/pdf",
                is_primary=True,
            )
        ],
        "source_query": " multi-agent   academic writing ",
        "retrieved_at": SNAPSHOT_AT,
    }
    payload.update(overrides)
    return PaperRecord(**payload)


def make_evidence(**overrides) -> EvidenceSpan:
    payload = {
        "source_id": "src_local_1",
        "artifact_sha256": "a" * 64,
        "page_start": 3,
        "page_end": 3,
        "char_start": 10,
        "char_end": 18,
        "chunk_id": "chunk_001",
        "exact_quote": "Evidence",
        "parser_version": "pdfplumber-1",
        "chunker_version": "page-v1",
        "embedding_model": "all-MiniLM-L6-v2",
        "embedding_version": "sentence-transformers-1",
        "index_version": "literature-index-v1",
    }
    payload.update(overrides)
    return EvidenceSpan(**payload)


class TestPaperRecord:
    def test_normalizes_identifiers_text_and_stable_identity(self) -> None:
        record = make_record()

        assert record.provider == "arxiv"
        assert record.title == "Multi-Agent Academic Writing"
        assert record.authors == ["Alice Example", "Bob Example"]
        assert record.categories == ["cs.AI", "cs.CL"]
        assert record.external_ids.doi == "10.1000/example"
        assert record.external_ids.arxiv == "2401.12345"
        assert record.external_ids.arxiv_version == 2
        assert record.paper_id.startswith("paper_")
        assert len(record.metadata_snapshot_hash) == 64

        same_record = make_record(retrieved_at=SNAPSHOT_AT + timedelta(days=1))
        assert same_record.paper_id == record.paper_id
        assert same_record.metadata_snapshot_hash == record.metadata_snapshot_hash

    def test_json_round_trip_preserves_contract(self) -> None:
        record = make_record()
        payload = record.model_dump(mode="json")

        assert isinstance(payload["retrieved_at"], str)
        assert PaperRecord.model_validate(payload) == record

    def test_rejects_tampered_snapshot_hash(self) -> None:
        payload = make_record().model_dump(mode="json")
        payload["metadata_snapshot_hash"] = "0" * 64

        with pytest.raises(ValidationError, match="does not match normalized metadata"):
            PaperRecord.model_validate(payload)

    def test_rejects_paper_id_that_differs_from_provider_identity(self) -> None:
        payload = make_record().model_dump(mode="json")
        payload["paper_id"] = "paper_wrong_identity"

        with pytest.raises(ValidationError, match="does not match provider identity"):
            PaperRecord.model_validate(payload)

    def test_snapshot_and_nested_collections_are_immutable(self) -> None:
        record = make_record()

        with pytest.raises(ValidationError, match="frozen"):
            record.title = "Changed after hashing"
        with pytest.raises(TypeError, match="immutable"):
            record.authors.append("Changed after hashing")
        with pytest.raises(TypeError, match="immutable"):
            record.external_ids.other["new"] = "identifier"

    def test_reinitializing_frozen_collections_is_blocked(self) -> None:
        record = make_record()

        with pytest.raises(TypeError, match="_build"):
            record.authors.__init__(["Z. Impostor"])
        with pytest.raises(TypeError, match="_build"):
            record.categories.__init__(["cs.AI"])
        with pytest.raises(TypeError, match="_build"):
            record.external_ids.other.__init__({"doi": "10.1000/other"})
        assert record.authors == ["Alice Example", "Bob Example"]
        assert record.external_ids.other == {}

    def test_model_copy_update_keeps_collections_frozen(self) -> None:
        record = make_record()

        copied = record.model_copy(update={"categories": ["cs.AI"], "authors": ["Solo Author"]})
        assert isinstance(copied.categories, list)
        with pytest.raises(TypeError, match="immutable"):
            copied.categories.append("cs.CL")
        with pytest.raises(TypeError, match="immutable"):
            copied.authors.append("Second Author")

        search = SearchQuery(query="multi-agent systems")
        filters_copy = search.filters.model_copy(update={"categories": ["cs.AI"]})
        assert list(filters_copy.categories) == ["cs.AI"]
        with pytest.raises(TypeError, match="immutable"):
            filters_copy.categories.append("cs.CL")

    def test_contract_models_are_hashable(self) -> None:
        record = make_record()

        assert hash(record) == hash(make_record())
        assert len({record, make_record()}) == 1
        assert len({SearchQuery(query="multi-agent systems"), SearchQuery(query="other")}) == 2

    def test_doi_normalization_accepts_publisher_forms(self) -> None:
        expected = "10.1000/example"

        for raw in (
            "10.1000/Example",
            "https://doi.org/10.1000/Example",
            "http://dx.doi.org/10.1000/Example",
            "https://www.doi.org/10.1000/Example",
            "DOI 10.1000/Example",
            "doi: 10.1000/Example",
            "info:doi/10.1000/Example",
            "https://doi.org/10.1000/Example?utm_source=arxiv",
            "https://doi.org/10.1000/Example#section",
        ):
            assert ExternalIdentifiers(doi=raw).doi == expected, raw

    def test_external_identifier_version_conflict_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="version conflicts"):
            ExternalIdentifiers(arxiv="2401.12345v2", arxiv_version=3)

    def test_extra_fields_are_rejected(self) -> None:
        payload = make_record().model_dump(mode="json")
        payload["unexpected"] = True

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PaperRecord.model_validate(payload)


class TestSearchContracts:
    def test_search_query_has_immutable_independent_filter_defaults(self) -> None:
        first = SearchQuery(query="  multi-agent   systems ")
        second = SearchQuery(query="other")

        with pytest.raises(TypeError, match="immutable"):
            first.filters.categories.append("cs.AI")
        assert first.query == "multi-agent systems"
        assert second.filters.categories == []

    def test_search_plan_records_confirmed_actual_query(self) -> None:
        plan = SearchPlan(
            research_question=" How are agents used in academic writing? ",
            suggested_query='all:"multi agent" AND all:"academic writing"',
            executed_query=SearchQuery(query='all:"multi agent" AND all:"academic writing"'),
            provider="ARXIV",
            generation_method=SearchGenerationMethod.LLM,
            generation_model="example-model",
            generation_config={"temperature": 0},
            confirmed_at=SNAPSHOT_AT,
            executed_at=SNAPSHOT_AT + timedelta(seconds=1),
            result_snapshot_id="search_000000000000000000000001",
        )

        assert plan.provider == "arxiv"
        assert plan.executed_query.query == 'all:"multi agent" AND all:"academic writing"'
        with pytest.raises(TypeError, match="immutable"):
            plan.generation_config["temperature"] = 1

    def test_search_plan_draft_records_generation_without_execution_fields(self) -> None:
        draft = SearchPlanDraft(
            research_question=" How are agents used in academic writing? ",
            suggested_query='all:"multi agent academic writing"',
            generation_method=SearchGenerationMethod.TEMPLATE,
            generation_config={"template": "all_phrase_v1"},
        )

        assert draft.research_question == "How are agents used in academic writing?"
        assert draft.suggested_query == 'all:"multi agent academic writing"'
        with pytest.raises(TypeError, match="immutable"):
            draft.generation_config["template"] = "changed"

        with pytest.raises(ValidationError, match="only valid"):
            SearchPlanDraft(
                research_question="question",
                suggested_query="query",
                generation_method=SearchGenerationMethod.TEMPLATE,
                generation_model="not-used",
            )

    def test_llm_search_plan_requires_model_and_aware_time(self) -> None:
        with pytest.raises(ValidationError, match="generation_model"):
            SearchPlan(
                research_question="question",
                suggested_query="query",
                executed_query=SearchQuery(query="query"),
                provider="arxiv",
                generation_method=SearchGenerationMethod.LLM,
                confirmed_at=SNAPSHOT_AT,
                executed_at=SNAPSHOT_AT + timedelta(seconds=1),
                result_snapshot_id="search_000000000000000000000001",
            )

        with pytest.raises(ValidationError, match="timezone"):
            SearchPlan(
                research_question="question",
                suggested_query="query",
                executed_query=SearchQuery(query="query"),
                provider="arxiv",
                generation_method=SearchGenerationMethod.USER,
                confirmed_at=datetime(2026, 9, 20),
                executed_at=SNAPSHOT_AT + timedelta(seconds=1),
                result_snapshot_id="search_000000000000000000000001",
            )

        with pytest.raises(ValidationError):
            SearchPlan(
                research_question="question",
                suggested_query="query",
                executed_query=SearchQuery(query="query"),
                provider="arxiv",
                generation_method=SearchGenerationMethod.USER,
                generation_config={"invalid": object()},
                confirmed_at=SNAPSHOT_AT,
                executed_at=SNAPSHOT_AT + timedelta(seconds=1),
                result_snapshot_id="search_000000000000000000000001",
            )

    def test_fixture_search_page_requires_label_and_has_stable_snapshot(self) -> None:
        record = make_record()
        query = SearchQuery(query="multi agent")

        with pytest.raises(ValidationError, match="provenance_label"):
            SearchPage(
                provider="fixture",
                result_mode=SearchResultMode.FIXTURE,
                query=query,
                records=[record],
                total_results=1,
                retrieved_at=SNAPSHOT_AT,
            )

        first = SearchPage(
            provider="fixture",
            result_mode=SearchResultMode.FIXTURE,
            query=query,
            records=[record],
            total_results=1,
            retrieved_at=SNAPSHOT_AT,
            provenance_label="fixture-v1",
        )
        second = SearchPage.model_validate(first.model_dump(mode="json"))
        assert first.result_snapshot_id == second.result_snapshot_id

    def test_search_page_rejects_contradictory_has_more(self) -> None:
        records = [make_record(), make_record(provider_record_id="2401.54321")]
        with pytest.raises(ValidationError, match="has_more contradicts"):
            SearchPage(
                provider="fixture",
                result_mode=SearchResultMode.FIXTURE,
                query=SearchQuery(query="multi agent", page_size=2),
                records=records,
                total_results=3,
                has_more=False,
                retrieved_at=SNAPSHOT_AT,
                provenance_label="fixture-v1",
            )


class TestFullTextArtifact:
    def test_ready_artifact_requires_file_identity(self) -> None:
        with pytest.raises(ValidationError, match="requires file fields"):
            FullTextArtifact(source_id="src_1", status=FullTextStatus.FULLTEXT_READY)

        artifact = FullTextArtifact(
            source_id="src_1",
            status=FullTextStatus.FULLTEXT_READY,
            local_path="D:/project/references/paper.pdf",
            source_url="https://arxiv.org/pdf/2401.12345",
            access_status=AccessStatus.OPEN,
            acquired_at=SNAPSHOT_AT,
            file_size_bytes=1234,
            mime_type="application/pdf",
            sha256="a" * 64,
        )

        assert artifact.artifact_id is not None
        assert artifact.artifact_id.startswith("artifact_")

    def test_ready_artifact_rejects_blank_or_empty_file_identity(self) -> None:
        with pytest.raises(ValidationError):
            FullTextArtifact(
                source_id="src_1",
                status=FullTextStatus.FULLTEXT_READY,
                local_path="   ",
                acquired_at=SNAPSHOT_AT,
                file_size_bytes=0,
                mime_type="   ",
                sha256="a" * 64,
            )

    def test_metadata_only_rejects_final_file_fields(self) -> None:
        with pytest.raises(ValidationError, match="cannot contain final file fields"):
            FullTextArtifact(
                source_id="src_1",
                status=FullTextStatus.METADATA_ONLY,
                local_path="D:/project/references/paper.pdf",
                acquired_at=SNAPSHOT_AT,
                file_size_bytes=1234,
                mime_type="application/pdf",
                sha256="a" * 64,
            )

    @pytest.mark.parametrize(
        "status",
        [
            FullTextStatus.ACCESS_UNAVAILABLE,
            FullTextStatus.ACQUIRE_FAILED,
        ],
    )
    def test_failure_state_requires_reason(self, status: FullTextStatus) -> None:
        with pytest.raises(ValidationError, match="requires failure_reason"):
            FullTextArtifact(source_id="src_1", status=status)

        state = FullTextArtifact(
            source_id="src_1",
            status=status,
            failure_reason="No lawful full-text location was available.",
        )
        assert state.artifact_id is None


class TestEvidenceAndClaims:
    def test_evidence_uses_one_based_single_page_exact_coordinates(self) -> None:
        evidence = make_evidence()

        assert evidence.page_start == 3
        assert evidence.page_end == 3
        assert evidence.quote_sha256
        assert evidence.evidence_id.startswith("evidence_")
        assert EvidenceSpan.model_validate(evidence.model_dump(mode="json")) == evidence

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"page_start": 0}, "greater than or equal to 1"),
            ({"page_end": 4}, "cannot cross PDF pages"),
            ({"char_end": 17}, "must exactly bound exact_quote"),
            ({"quote_sha256": "0" * 64}, "does not match exact_quote"),
        ],
    )
    def test_invalid_evidence_is_rejected(self, overrides: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            make_evidence(**overrides)

    def test_claim_requires_evidence_or_explicit_insufficiency(self) -> None:
        common = {
            "text": "The selected corpus supports this route.",
            "model_provider": "ollama",
            "model_name": "example-model",
            "model_config_hash": "b" * 64,
            "generated_at": SNAPSHOT_AT,
        }

        with pytest.raises(ValidationError, match="require evidence_ids"):
            AnswerClaim(evidence_status=EvidenceStatus.SUPPORTED, **common)

        insufficient = AnswerClaim(
            evidence_status=EvidenceStatus.INSUFFICIENT,
            evidence_ids=[],
            **common,
        )
        supported = AnswerClaim(
            evidence_status=EvidenceStatus.SUPPORTED,
            evidence_ids=[make_evidence().evidence_id],
            **common,
        )

        assert insufficient.evidence_ids == []
        assert insufficient.claim_id.startswith("claim_")
        assert AnswerClaim.model_validate(supported.model_dump(mode="json")) == supported

    def test_claim_rejects_duplicate_evidence_ids(self) -> None:
        evidence_id = make_evidence().evidence_id
        with pytest.raises(ValidationError, match="cannot contain duplicates"):
            AnswerClaim(
                text="Duplicated evidence is invalid.",
                evidence_ids=[evidence_id, evidence_id],
                evidence_status=EvidenceStatus.SUPPORTED,
                model_provider="ollama",
                model_name="example-model",
                model_config_hash="b" * 64,
                generated_at=SNAPSHOT_AT,
            )
