"""SemanticAgent fallback behavior tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.semantic.agent import (
    CANARY_EXPERIMENTAL_MODE,
    DEFAULT_RETRIEVAL_MODE,
    HYBRID_CANARY_FALLBACK_MODE,
    SemanticAgent,
)
from retrieval.pipeline import RetrievalMode, RetrievalResult


def _pipe_factory(primary_pipe, fallback_pipe):
    def factory(session, *, config=None):
        if config and config.mode == HYBRID_CANARY_FALLBACK_MODE:
            return fallback_pipe
        return primary_pipe

    return factory


def test_default_mode_is_evidence_candidates_without_fallback():
    agent = SemanticAgent(MagicMock(), use_isolated_session=False)
    assert agent.retrieval_mode == DEFAULT_RETRIEVAL_MODE
    assert agent.retrieval_mode == RetrievalMode.EVIDENCE_CANDIDATES
    assert agent.fallback_mode is None


def test_semantic_agent_falls_back_on_primary_error():
    session = MagicMock()
    primary_pipe = MagicMock(session=session)
    primary_pipe.retrieve = MagicMock(side_effect=RuntimeError("lexical index down"))
    fallback = RetrievalResult(
        chunks=[{"chunk_id": "semantic_021_01", "page_number": 21, "score": 0.9}],
        mode=RetrievalMode.VECTOR_METADATA.value,
        trace=["mode:vector_metadata"],
    )
    fallback_pipe = MagicMock()
    fallback_pipe.retrieve = MagicMock(return_value=fallback)

    with patch(
        "agents.semantic.agent.ProductionRetrievalPipeline",
        side_effect=_pipe_factory(primary_pipe, fallback_pipe),
    ):
        agent = SemanticAgent(
            session,
            retrieval_mode=CANARY_EXPERIMENTAL_MODE,
            fallback_mode=HYBRID_CANARY_FALLBACK_MODE,
            use_isolated_session=False,
        )
        result = agent._retrieve_with_fallback(primary_pipe, "query", page_hint=21)

    assert result.success is True
    assert result.chunks[0]["chunk_id"] == "semantic_021_01"
    assert any("fallback:primary_error" in t for t in result.retrieval_trace)


def test_semantic_agent_falls_back_on_empty_with_page_hint():
    session = MagicMock()
    primary = RetrievalResult(chunks=[], mode=CANARY_EXPERIMENTAL_MODE.value, trace=["mode:hybrid_rerank"])
    primary_pipe = MagicMock(session=session)
    primary_pipe.retrieve = MagicMock(return_value=primary)
    fallback = RetrievalResult(
        chunks=[{"chunk_id": "semantic_021_01", "page_number": 21, "score": 0.9}],
        mode=RetrievalMode.VECTOR_METADATA.value,
        trace=["mode:vector_metadata"],
    )
    fallback_pipe = MagicMock()
    fallback_pipe.retrieve = MagicMock(return_value=fallback)

    with patch(
        "agents.semantic.agent.ProductionRetrievalPipeline",
        side_effect=_pipe_factory(primary_pipe, fallback_pipe),
    ):
        agent = SemanticAgent(
            session,
            retrieval_mode=CANARY_EXPERIMENTAL_MODE,
            fallback_mode=HYBRID_CANARY_FALLBACK_MODE,
            use_isolated_session=False,
        )
        result = agent._retrieve_with_fallback(primary_pipe, "query", page_hint=21)

    assert result.success is True
    assert any("fallback:empty_with_page_hint" in t for t in result.retrieval_trace)
