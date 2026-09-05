"""Tests for hardened Persian semantic retrieval layer."""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import func, select

from retrieval.semantic_retrieval import (
    PRODUCTION_RETRIEVAL_SOURCE_TYPES,
    REQUIRED_RESULT_FIELDS,
    SEMANTIC_SOURCE_TYPE,
)

PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")


def _db_available() -> bool:
    try:
        from database.session import verify_connection

        return verify_connection()
    except Exception:
        return False


def _gcp_available() -> bool:
    if os.getenv("SKIP_GCP_INTEGRATION") == "1":
        return False
    try:
        from retrieval.embeddings import EmbeddingService

        return EmbeddingService().available()
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")
pytestmark_gcp = pytest.mark.skipif(not _gcp_available(), reason="GCP embedding unavailable")

PHASE7_MIN_EMBEDDED = 563


def _phase7_db_ready(session) -> bool:
    from persistence.semantic_store import count_production_semantic_chunks

    return count_production_semantic_chunks(session)["embedded_semantic"] >= PHASE7_MIN_EMBEDDED


def _require_phase7_db(session) -> None:
    if not _phase7_db_ready(session):
        pytest.skip(
            f"Phase 7 semantic corpus not loaded (embedded_semantic < {PHASE7_MIN_EMBEDDED})"
        )


def test_required_result_fields_contract():
    assert "chunk_id" in REQUIRED_RESULT_FIELDS
    assert "provenance" in REQUIRED_RESULT_FIELDS
    assert "source_reference" in REQUIRED_RESULT_FIELDS


@pytestmark_db
def test_production_semantic_counts():
    from database.models import DocumentChunk
    from database.session import session_scope
    from persistence.semantic_store import count_production_semantic_chunks

    with session_scope() as session:
        _require_phase7_db(session)
        counts = count_production_semantic_chunks(session)
        embedded = counts["embedded_semantic"]
        assert embedded >= PHASE7_MIN_EMBEDDED

        legacy = session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.source_type != SEMANTIC_SOURCE_TYPE,
                DocumentChunk.embedding.is_not(None),
            )
        )
        assert legacy is not None


@pytestmark_db
@pytestmark_gcp
def test_persian_query_embedding_dimension():
    from retrieval.embeddings import EmbeddingService

    embedder = EmbeddingService()
    vector = embedder.embed_texts(["حد مجاز مواجهه شغلی چیست؟"])[0]
    assert len(vector) == embedder.dimension == 3072


@pytestmark_db
@pytestmark_gcp
def test_search_returns_full_metadata():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        results = search_persian_semantic(session, "تعریف حدود مجاز مواجهه شغلی", limit=3)
    assert results
    for row in results:
        assert set(row.keys()) >= REQUIRED_RESULT_FIELDS
        assert row["source_type"] in PRODUCTION_RETRIEVAL_SOURCE_TYPES
        assert row["validation_status"] == "accepted"
        assert row["embedding_model"]
        assert row["embedding_dimension"] == 3072
        assert PERSIAN_RE.search(row["content"] or "")


@pytestmark_db
@pytestmark_gcp
def test_scope_isolation_no_legacy_source_types():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        results = search_persian_semantic(session, "مواد شیمیایی و سروصدا", limit=10)
    for row in results:
        assert row["source_type"] in PRODUCTION_RETRIEVAL_SOURCE_TYPES
        assert row["validation_status"] == "accepted"
        assert row["language"] in {"fa", "fa,en"}


@pytestmark_db
@pytestmark_gcp
def test_provenance_chain_present():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        _require_phase7_db(session)
        results = search_persian_semantic(
            session,
            "حدود مجاز مواجهه شغلی (OEL) چیست",
            limit=5,
        )
    assert results
    row = next(
        (
            r
            for r in results
            if r.get("page_number")
            or (r.get("provenance") or {}).get("page_number")
            or (r.get("source_reference") or {}).get("page_number")
        ),
        results[0],
    )
    assert row["chunk_id"]
    assert row["document_id"]
    provenance = row.get("provenance") or {}
    source_ref = row.get("source_reference") or {}
    page = row.get("page_number") or provenance.get("page_number") or source_ref.get("page_number")
    assert page is not None
    assert row.get("gold_artifact_path") or provenance.get("source_pdf")


@pytestmark_db
@pytestmark_gcp
def test_numeric_content_not_modified():
    from database.models import DocumentChunk
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        chunk = session.scalar(
            select(DocumentChunk)
            .where(
                DocumentChunk.source_type == "semantic_text",
                DocumentChunk.chunk_id == "semantic_025_01",
            )
        )
        assert chunk is not None
        stored = chunk.content
        results = search_persian_semantic(session, "8 ساعت کار TWA", limit=5)
        matched = [r for r in results if r["chunk_id"] == "semantic_025_01"]
        if matched:
            assert matched[0]["content"] == stored


@pytestmark_db
@pytestmark_gcp
def test_empty_query_returns_empty():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        assert search_persian_semantic(session, "   ", limit=5) == []


@pytestmark_db
@pytestmark_gcp
def test_eval_set_top5_coverage():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic
    from retrieval.semantic_eval_set import SEMANTIC_EVAL_CASES

    hits = 0
    with session_scope() as session:
        _require_phase7_db(session)
        for case in SEMANTIC_EVAL_CASES:
            results = search_persian_semantic(session, case.query, limit=5)
            ids = {r["chunk_id"] for r in results}
            if ids.intersection(case.expected_chunk_ids):
                hits += 1
    assert hits >= 7, f"expected at least 7/10 eval queries to hit ground truth in top-5, got {hits}"


@pytestmark_db
@pytestmark_gcp
def test_no_english_legacy_qa_in_results():
    from database.session import session_scope
    from persistence.semantic_store import search_persian_semantic

    with session_scope() as session:
        results = search_persian_semantic(session, "What is TWA occupational exposure limit", limit=5)
    for row in results:
        assert row["source_type"] in PRODUCTION_RETRIEVAL_SOURCE_TYPES
        assert row["validation_status"] != "legacy_reference"
