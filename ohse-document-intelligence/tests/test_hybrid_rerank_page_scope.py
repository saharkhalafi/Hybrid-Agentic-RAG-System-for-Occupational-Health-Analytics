"""Regression tests for hybrid rerank page-scoping fix."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from retrieval.pipeline import (
    ProductionRetrievalPipeline,
    RetrievalConfig,
    RetrievalMode,
    _apply_page_scope,
    _metadata_filter,
)


def test_metadata_filter_keeps_page_matches():
    candidates = [
        {"chunk_id": "a", "page_number": 21},
        {"chunk_id": "b", "page_number": 378},
        {"chunk_id": "c", "page_number": 21},
    ]
    filtered = _metadata_filter(candidates, 21)
    assert [c["chunk_id"] for c in filtered] == ["a", "c"]


def test_metadata_filter_falls_back_when_no_page_matches():
    candidates = [
        {"chunk_id": "a", "page_number": 378},
        {"chunk_id": "b", "page_number": 400},
    ]
    filtered = _metadata_filter(candidates, 21)
    assert filtered == candidates


def test_apply_page_scope_filters_both_paths_identically():
    vector = [
        {"chunk_id": "v21", "page_number": 21},
        {"chunk_id": "v378", "page_number": 378},
    ]
    lexical = [
        {"chunk_id": "l21", "page_number": 21},
        {"chunk_id": "l378", "page_number": 378},
    ]
    scoped_v, scoped_l = _apply_page_scope(vector, lexical, 21)
    assert {c["chunk_id"] for c in scoped_v} == {"v21"}
    assert {c["chunk_id"] for c in scoped_l} == {"l21"}


def test_lexical_path_respects_page_hint():
    """Lexical candidates must be page-scoped like vector before merge/rerank."""
    session = MagicMock()
    vector_hits = [
        {
            "chunk_id": "semantic_021_01",
            "page_number": 21,
            "content": "page 21 vector",
            "score": 0.85,
        },
        {
            "chunk_id": "semantic_378_02",
            "page_number": 378,
            "content": "page 378 vector",
            "score": 0.95,
        },
    ]
    lexical_hits = [
        {
            "chunk_id": "semantic_378_02",
            "page_number": 378,
            "content": "page 378 lexical pollution",
            "score": 99.0,
            "enriched_content": "page 378 lexical pollution",
        },
        {
            "chunk_id": "semantic_021_01",
            "page_number": 21,
            "content": "page 21 lexical",
            "score": 50.0,
            "enriched_content": "page 21 lexical",
        },
    ]

    with (
        patch("retrieval.pipeline.search_by_query_vector", return_value=vector_hits),
        patch("retrieval.pipeline.embed_query_vector", return_value=[0.0] * 8),
        patch("retrieval.pipeline.get_lexical_index") as mock_lex_index,
    ):
        mock_lex_index.return_value.search.return_value = lexical_hits
        pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(
                mode=RetrievalMode.HYBRID_RERANK,
                candidate_k=30,
                final_k=5,
                reranker=None,
            ),
        )
        result = pipe.retrieve("test query", page_hint=21)

    chunk_ids = [c.get("chunk_id") for c in result.chunks]
    pages = [c.get("page_number") for c in result.chunks]
    assert "semantic_378_02" not in chunk_ids
    assert chunk_ids[0] == "semantic_021_01"
    assert all(p == 21 for p in pages if p is not None)
    assert "page_scope_filter" in result.trace


def test_vector_lexical_mode_applies_page_scope_to_both_paths():
    """VECTOR_LEXICAL had the same asymmetry class; both pools must be scoped."""
    session = MagicMock()
    vector_hits = [
        {"chunk_id": "v378", "page_number": 378, "content": "wrong page", "score": 0.99},
        {"chunk_id": "v21", "page_number": 21, "content": "right page", "score": 0.80},
    ]
    lexical_hits = [
        {"chunk_id": "l378", "page_number": 378, "content": "lex wrong", "score": 100.0},
        {"chunk_id": "l21", "page_number": 21, "content": "lex right", "score": 10.0},
    ]

    with (
        patch("retrieval.pipeline.search_by_query_vector", return_value=vector_hits),
        patch("retrieval.pipeline.embed_query_vector", return_value=[0.0] * 8),
        patch("retrieval.pipeline.get_lexical_index") as mock_lex_index,
    ):
        mock_lex_index.return_value.search.return_value = lexical_hits
        pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(
                mode=RetrievalMode.VECTOR_LEXICAL,
                candidate_k=30,
                final_k=5,
                reranker=None,
            ),
        )
        result = pipe.retrieve("test query", page_hint=21)

    chunk_ids = [c.get("chunk_id") for c in result.chunks]
    pages = [c.get("page_number") for c in result.chunks]
    assert "v378" not in chunk_ids
    assert "l378" not in chunk_ids
    assert all(p == 21 for p in pages if p is not None)
    assert "page_scope_filter" in result.trace
