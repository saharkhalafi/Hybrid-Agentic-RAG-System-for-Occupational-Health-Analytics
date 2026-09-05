"""Regression: test fixtures stay out of ANN; PDF vs printed page identity."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.evaluation.goldset_qa_relevance import pages_match
from persistence.semantic_store import (
    _exclude_test_fixture_clause,
    _is_production_retrieval_chunk,
    search_by_query_vector,
)
from retrieval.page_identity import canonical_document_page, retrieval_page_fields
from retrieval.pipeline import (
    ProductionRetrievalPipeline,
    RetrievalConfig,
    RetrievalMode,
    _candidate_to_result,
    _filter_test_chunks,
    _metadata_filter,
)
from retrieval.reranker import RankedCandidate
from retrieval.semantic_retrieval import (
    PRODUCTION_RETRIEVAL_LANGUAGES,
    PRODUCTION_RETRIEVAL_SOURCE_TYPES,
    ROW_KNOWLEDGE_SOURCE_TYPE,
    SEMANTIC_LANGUAGE,
    SEMANTIC_SOURCE_TYPE,
    TEST_CHUNK_ID_PREFIX,
)


def test_canonical_page_prefers_printed_when_pdf_differs_by_one():
    assert canonical_document_page(page_number=22, printed_page_number=21) == 21
    fields = retrieval_page_fields(page_number=22, printed_page_number=21)
    assert fields["page_number"] == 22
    assert fields["printed_page_number"] == 21
    assert fields["document_page"] == 21
    assert pages_match(fields, 21) is True
    assert pages_match(fields, 22) is True


def test_canonical_page_falls_back_to_pdf_when_printed_missing():
    assert canonical_document_page(page_number=22, printed_page_number=None) == 22
    fields = retrieval_page_fields(page_number=22, printed_page_number=None)
    assert pages_match(fields, 22) is True
    assert pages_match(fields, 21) is False


def test_metadata_filter_matches_printed_folio_not_only_pdf_index():
    candidates = [
        {"chunk_id": "a", "page_number": 22, "printed_page_number": 21},
        {"chunk_id": "b", "page_number": 378, "printed_page_number": 377},
    ]
    filtered = _metadata_filter(candidates, 21)
    assert [c["chunk_id"] for c in filtered] == ["a"]


def test_candidate_to_result_keeps_both_page_identities():
    cand = RankedCandidate(
        chunk_id="semantic_022_01",
        content="body",
        score=0.9,
        metadata={"page_number": 22, "printed_page_number": 21, "section_title": "intro"},
    )
    out = _candidate_to_result(cand)
    assert out["page_number"] == 22
    assert out["printed_page_number"] == 21
    assert out["document_page"] == 21
    assert pages_match(out, 21) is True
    assert pages_match(out, 22) is True


def test_ann_sql_excludes_test_persian_prefix():
    compiled = str(_exclude_test_fixture_clause().compile(compile_kwargs={"literal_binds": True}))
    assert TEST_CHUNK_ID_PREFIX in compiled
    assert "NOT" in compiled.upper() or "not" in compiled.lower()

    session = MagicMock()
    session.execute.return_value.all.return_value = []
    search_by_query_vector(session, [0.0] * 8, limit=5)
    stmt = session.execute.call_args[0][0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "test_persian_" in sql


def test_ann_sql_includes_embedded_row_knowledge_with_production_filters():
    session = MagicMock()
    session.execute.return_value.all.return_value = []
    search_by_query_vector(session, [0.0] * 8, limit=5)
    stmt = session.execute.call_args[0][0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert SEMANTIC_SOURCE_TYPE in sql
    assert ROW_KNOWLEDGE_SOURCE_TYPE in sql
    assert "accepted" in sql
    assert "embedding" in sql
    assert "is not null" in sql
    for language in PRODUCTION_RETRIEVAL_LANGUAGES:
        assert language in sql
    assert "test_persian_" in sql
    assert "evidence_cell" not in sql


def test_production_retrieval_accepts_row_knowledge_and_rejects_other_sources():
    ok_row = MagicMock(
        source_type=ROW_KNOWLEDGE_SOURCE_TYPE,
        validation_status="accepted",
        language="fa,en",
        chunk_id="row_table_090_101_003",
    )
    ok_semantic = MagicMock(
        source_type=SEMANTIC_SOURCE_TYPE,
        validation_status="accepted",
        language=SEMANTIC_LANGUAGE,
        chunk_id="semantic_090_01",
    )
    legacy = MagicMock(
        source_type="evidence_cell",
        validation_status="accepted",
        language="fa,en",
        chunk_id="cell_1",
    )
    assert _is_production_retrieval_chunk(ok_row) is True
    assert _is_production_retrieval_chunk(ok_semantic) is True
    assert _is_production_retrieval_chunk(legacy) is False
    assert PRODUCTION_RETRIEVAL_SOURCE_TYPES == (SEMANTIC_SOURCE_TYPE, ROW_KNOWLEDGE_SOURCE_TYPE)


def test_search_dedupes_duplicate_chunk_ids_preserving_rank_order():
    first = MagicMock(
        source_type=ROW_KNOWLEDGE_SOURCE_TYPE,
        validation_status="accepted",
        language="fa,en",
        chunk_id="row_table_090_101_003",
        document_id="doc-1",
        content="chemical_name: EPN",
        page_number=90,
        printed_page_number=None,
        section_id=None,
        section_title=None,
        chunk_type=None,
        topic="row_knowledge",
        embedding_model="gemini-embedding-001",
        embedding_version="1",
        embedding_dimension=3072,
        content_version="1",
        provenance={},
        metadata_={},
        gold_artifact_path="canonical_evidence_v1",
    )
    duplicate = MagicMock(
        source_type=ROW_KNOWLEDGE_SOURCE_TYPE,
        validation_status="accepted",
        language="fa,en",
        chunk_id="row_table_090_101_003",
        document_id="doc-1",
        content="chemical_name: EPN",
        page_number=90,
        printed_page_number=None,
        section_id=None,
        section_title=None,
        chunk_type=None,
        topic="row_knowledge",
        embedding_model="gemini-embedding-001",
        embedding_version="1",
        embedding_dimension=3072,
        content_version="1",
        provenance={},
        metadata_={},
        gold_artifact_path="canonical_evidence_v1",
    )
    other = MagicMock(
        source_type=SEMANTIC_SOURCE_TYPE,
        validation_status="accepted",
        language=SEMANTIC_LANGUAGE,
        chunk_id="semantic_090_01",
        document_id="doc-1",
        content="narrative",
        page_number=90,
        printed_page_number=None,
        section_id=None,
        section_title=None,
        chunk_type=None,
        topic="semantic_text",
        embedding_model="gemini-embedding-001",
        embedding_version="1",
        embedding_dimension=3072,
        content_version="1",
        provenance={},
        metadata_={},
        gold_artifact_path="gold",
    )
    session = MagicMock()
    session.execute.return_value.all.return_value = [
        (first, 0.9),
        (duplicate, 0.8),
        (other, 0.7),
    ]
    results = search_by_query_vector(session, [0.0] * 8, limit=5)
    assert [row["chunk_id"] for row in results] == [
        "row_table_090_101_003",
        "semantic_090_01",
    ]
    assert [row["source_type"] for row in results] == [
        ROW_KNOWLEDGE_SOURCE_TYPE,
        SEMANTIC_SOURCE_TYPE,
    ]


def test_test_fixtures_cannot_enter_vector_metadata_candidate_set():
    session = MagicMock()
    vector_hits = [
        {"chunk_id": f"{TEST_CHUNK_ID_PREFIX}aabbccdd", "page_number": 1, "content": "fixture", "score": 0.99},
        {
            "chunk_id": "semantic_023_01",
            "page_number": 23,
            "printed_page_number": 22,
            "content": "production",
            "score": 0.50,
        },
    ]
    with (
        patch("retrieval.pipeline.search_by_query_vector", return_value=vector_hits),
        patch("retrieval.pipeline.embed_query_vector", return_value=[0.0] * 8),
    ):
        pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(mode=RetrievalMode.VECTOR_METADATA, candidate_k=30, final_k=5),
        )
        result = pipe.retrieve("query")

    ids = [c.get("chunk_id") for c in result.chunks]
    assert all(not str(cid).startswith(TEST_CHUNK_ID_PREFIX) for cid in ids)
    assert "semantic_023_01" in ids
    assert result.chunks[0]["printed_page_number"] == 22


def test_vector_metadata_ranking_unchanged_without_fixtures():
    session = MagicMock()
    vector_hits = [
        {"chunk_id": "semantic_a", "page_number": 10, "printed_page_number": 9, "content": "a", "score": 0.9},
        {"chunk_id": "semantic_b", "page_number": 11, "printed_page_number": 10, "content": "b", "score": 0.8},
        {"chunk_id": "semantic_c", "page_number": 12, "printed_page_number": 11, "content": "c", "score": 0.7},
    ]
    with (
        patch("retrieval.pipeline.search_by_query_vector", return_value=vector_hits),
        patch("retrieval.pipeline.embed_query_vector", return_value=[0.0] * 8),
    ):
        pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(mode=RetrievalMode.VECTOR_METADATA, candidate_k=30, final_k=2),
        )
        result = pipe.retrieve("query")

    assert [c["chunk_id"] for c in result.chunks] == ["semantic_a", "semantic_b"]
    assert [c["page_number"] for c in result.chunks] == [10, 11]


def test_post_filter_still_drops_test_prefix():
    rows = [
        {"chunk_id": f"{TEST_CHUNK_ID_PREFIX}x", "content": "x"},
        {"chunk_id": "semantic_001_01", "content": "ok"},
    ]
    assert [r["chunk_id"] for r in _filter_test_chunks(rows, True)] == ["semantic_001_01"]
