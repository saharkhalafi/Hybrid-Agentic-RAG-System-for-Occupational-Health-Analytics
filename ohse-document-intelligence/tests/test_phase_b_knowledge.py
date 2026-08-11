"""Phase B knowledge engineering tests."""

from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, func, text

ROOT = Path(__file__).resolve().parents[1]
GOLD_TABLES = ROOT / "gold" / "tables"
GOLD_FORMULAS = ROOT / "gold" / "formulas"
GOLD_CANDIDATES = ROOT / "gold" / "candidates" / "tables"


# ---------------------------------------------------------------------------
# Unit tests (no DB)
# ---------------------------------------------------------------------------


def test_metadata_contract_validation():
    from knowledge.metadata_contract import KnowledgeMetadata, SourceReference, validate_knowledge_metadata

    meta = KnowledgeMetadata(
        record_type="structured",
        record_id="table_055_01:row_1",
        document_id=str(uuid.uuid4()),
        source_reference=SourceReference(table_id="table_055_01", page_number=55),
    )
    assert validate_knowledge_metadata(meta) == []


def test_candidate_path_rejected():
    from persistence.knowledge_pipeline import is_production_gold_table, reject_candidate_sync

    assert reject_candidate_sync(GOLD_CANDIDATES / "table_056_01.json") is True
    payload = {"gold_allowed": True, "table_type": "chemical_oel"}
    allowed, reason = is_production_gold_table(payload, GOLD_CANDIDATES / "fake.json")
    assert allowed is False
    assert reason == "candidate_path_forbidden"


def test_gold_allowed_gate():
    from persistence.knowledge_pipeline import is_production_gold_table

    path = GOLD_TABLES / "table_055_01.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed, _ = is_production_gold_table(payload, path)
    assert allowed is True
    assert payload.get("gold_allowed") is True


def test_numeric_integrity_original_vs_normalized():
    """Verify real OHE6 values where original_value != normalized_value."""
    path = GOLD_TABLES / "table_055_01.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mismatches = []
    for row in payload.get("rows", []):
        for field in ("TWA", "STEL", "STEL/C"):
            fdata = row.get(field)
            if not isinstance(fdata, dict):
                continue
            orig = fdata.get("original_value")
            norm = fdata.get("normalized_value") or fdata.get("value")
            if orig and norm and str(orig).strip() != str(norm).strip():
                mismatches.append((field, orig, norm))
    assert len(mismatches) >= 3, "expected several original≠normalized OHE6 fields"
    assert any("mg/m" in str(m[1]) for m in mismatches)


def test_formula_deterministic_calculation():
    from knowledge.calculation import evaluate_vibration_daily_exposure

    result = evaluate_vibration_daily_exposure(
        formula_id="formula_240_01",
        ahw_values=[2.0, 3.0],
        t_values=[4.0, 6.0],
    )
    expected = math.sqrt((1.0 / 10.0) * (4.0 * 4 + 9.0 * 6))
    assert result.valid is True
    assert math.isclose(result.result, expected, rel_tol=1e-9)


def test_semantic_review_required_rejected_from_production():
    from persistence.semantic_store import is_production_semantic_record

    record = {"chunk_type": "semantic_text", "review_status": "review_required", "text": "حد مجاز"}
    allowed, reason = is_production_semantic_record(record)
    assert allowed is False
    assert "review_status" in reason


def test_semantic_accepted_for_production():
    from persistence.semantic_store import is_production_semantic_record

    record = {"chunk_type": "semantic_text", "review_status": "accepted", "text": "حد مجاز مواجهه"}
    allowed, _ = is_production_semantic_record(record)
    assert allowed is True


def test_semantic_promotion_accepts_pending():
    from persistence.semantic_gold_promotion import promote_semantic_gold_for_production
    from config.settings import get_settings

    stats = promote_semantic_gold_for_production(settings=get_settings(), force=True)
    assert stats.promoted >= 500
    production = get_settings().gold_dir / "rag" / "semantic_text_production.jsonl"
    assert production.exists()


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    try:
        from database.session import verify_connection

        verify_connection()
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")


@pytestmark_db
def test_pgvector_extension():
    from database.session import engine

    with engine.connect() as conn:
        row = conn.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'")).first()
    assert row is not None


@pytestmark_db
def test_phase_b_tables_exist():
    from database.session import engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for required in (
        "documents",
        "document_pages",
        "chemical_registry",
        "oel_chemical_limits",
        "noise_limits",
        "vibration_limits",
        "biological_exposure_limits",
        "regulatory_constraints",
        "document_chunks",
        "formulas",
        "knowledge_sync_runs",
    ):
        assert required in tables


@pytestmark_db
def test_gold_sync_idempotent():
    from database.session import session_scope
    from persistence.knowledge_pipeline import sync_gold_tables

    with session_scope() as session:
        stats1 = sync_gold_tables(session)
        stats2 = sync_gold_tables(session)
    assert stats1.tables_synced > 0
    assert stats2.rows_synced == 0
    assert stats2.duplicates_prevented > 0


@pytestmark_db
def test_semantic_production_sync():
    from database.models import DocumentChunk
    from database.session import session_scope
    from persistence.semantic_gold_promotion import promote_semantic_gold_for_production
    from persistence.semantic_store import SEMANTIC_SOURCE_TYPE, sync_semantic_production_corpus

    promote_semantic_gold_for_production(force=True)
    with session_scope() as session:
        stats = sync_semantic_production_corpus(session)
        count = session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
                DocumentChunk.validation_status == "accepted",
            )
        )
    assert stats.chunks_synced >= 500
    assert count >= 500


@pytestmark_db
def test_candidate_not_synced():
    from persistence.knowledge_pipeline import reject_candidate_sync

    if GOLD_CANDIDATES.exists():
        for path in GOLD_CANDIDATES.glob("*.json"):
            assert reject_candidate_sync(path) is True


@pytestmark_db
def test_formula_registry_sync():
    from database.models import Formula
    from database.session import session_scope
    from persistence.formula_registry import sync_gold_formulas

    with session_scope() as session:
        stats = sync_gold_formulas(session)
        formulas = session.scalars(select(Formula).where(Formula.validation_status == "accepted")).all()
    assert stats.synced + stats.duplicates_prevented >= len(list(GOLD_FORMULAS.glob("*.json")))
    assert len(formulas) >= 1
    sample = formulas[0]
    assert sample.stable_formula_id
    assert sample.normalized_expression


@pytestmark_db
def test_provenance_chain_e2e():
    from database.models import OELChemicalLimit
    from database.session import session_scope
    from knowledge.metadata_contract import build_provenance_chain, KnowledgeMetadata
    from persistence.knowledge_pipeline import sync_gold_tables

    with session_scope() as session:
        sync_gold_tables(session)
        limit = session.scalars(
            select(OELChemicalLimit).where(OELChemicalLimit.source_row_key.like("table_055_01:%"))
        ).first()
        assert limit is not None
        meta_dict = limit.knowledge_metadata or {}
        meta = KnowledgeMetadata(
            record_type="structured",
            record_id=limit.source_row_key or "",
            document_id=meta_dict.get("document_id", ""),
            gold_artifact_path=limit.gold_artifact_path,
            source_reference=__import__("knowledge.metadata_contract", fromlist=["SourceReference"]).SourceReference(
                **(meta_dict.get("source_reference") or {})
            ),
        )
        twa_orig = (limit.original_values or {}).get("TWA", {}).get("original_value")
        twa_norm = (limit.original_values or {}).get("TWA", {}).get("normalized_value")
        chain = build_provenance_chain(
            fact=f"TWA={limit.twa}",
            metadata=meta,
            original_value=twa_orig,
            normalized_value=twa_norm,
        )
    assert chain.gold_artifact_path
    assert chain.table_id == "table_055_01"
    assert chain.page_number == 55
    assert chain.original_value != chain.normalized_value or twa_orig is None


def _gcp_available() -> bool:
    if os.getenv("SKIP_GCP_INTEGRATION") == "1":
        return False
    try:
        from retrieval.embeddings import EmbeddingService

        return EmbeddingService().available()
    except Exception:
        return False


@pytest.mark.skipif(not _gcp_available(), reason="GCP embedding client unavailable")
@pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")
def test_gemini_persian_embedding_and_retrieval():
    from database.models import ChunkType, Document, DocumentChunk
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic
    from retrieval.embeddings import EmbeddingService

    persian_text = "حد مجاز مواجهه شغلی برای عوامل شیمیایی در محیط کار"
    embedder = EmbeddingService()
    vectors = embedder.embed_texts([persian_text])
    assert len(vectors) == 1
    assert len(vectors[0]) == embedder.dimension

    with session_scope() as session:
        doc = Document(
            filename="phase_b_test.pdf",
            content_hash=f"test_{uuid.uuid4().hex}",
            language="fa",
            processing_version="test",
        )
        session.add(doc)
        session.flush()

        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_id=f"test_persian_{uuid.uuid4().hex[:8]}",
            content=persian_text,
            chunk_type=ChunkType.PARAGRAPH,
            language="fa",
            source_type="semantic_text",
            validation_status="accepted",
            embedding=vectors[0],
            embedding_model=embedder.model_name,
            embedding_dimension=embedder.dimension,
            embedding_version="test",
            content_version="test",
        )
        session.add(chunk)
        session.flush()

        results = search_persian_semantic(session, "حد مجاز مواجهه شیمیایی", limit=3)
    assert len(results) >= 1
    assert results[0]["score"] > 0.5
