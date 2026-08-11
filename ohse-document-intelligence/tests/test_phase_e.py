"""Phase E — Performance & scalability optimization tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from agents.orchestrator.pipeline import QueryOrchestrator
from agents.semantic.agent import SemanticAgent
from config.settings import get_settings
from database.session import pool_stats
from persistence.semantic_store import embed_query_vector, search_by_query_vector
from retrieval.embeddings import EmbeddingService, get_embedding_service


class TestPoolConfiguration:
    def test_pool_settings_defaults(self):
        s = get_settings()
        assert s.postgres_pool_size >= 30
        assert s.postgres_max_overflow >= 50
        assert s.postgres_pool_timeout >= 30

    def test_pool_stats_exposes_max_connections(self):
        stats = pool_stats()
        assert stats["max_connections"] == stats["pool_size"] + stats["max_overflow"]
        assert "checked_out" in stats


class TestEmbeddingSingleton:
    def test_get_embedding_service_is_singleton(self):
        a = get_embedding_service()
        b = get_embedding_service()
        assert a is b
        assert isinstance(a, EmbeddingService)


class TestSemanticSessionIsolation:
    def test_semantic_agent_default_isolated(self):
        agent = SemanticAgent()
        assert agent.use_isolated_session is True
        assert agent.pipeline is None

    @patch("persistence.semantic_store.embed_query_vector")
    @patch("database.session.SessionLocal")
    @patch("retrieval.pipeline.ProductionRetrievalPipeline.retrieve")
    def test_isolated_execute_closes_session(
        self, mock_retrieve, mock_session_local, mock_embed
    ):
        mock_embed.return_value = [0.1] * get_settings().vector_dimension
        mock_db = MagicMock(spec=Session)
        mock_session_local.return_value = mock_db
        mock_retrieve.return_value = MagicMock(
            chunks=[{"chunk_id": "c1", "score": 0.9}],
            latency_ms=12.0,
            mode="hybrid_rerank",
            trace=["vector:1"],
        )

        agent = SemanticAgent()
        result = agent.execute("تعریف TWA چیست؟")

        assert result.success is True
        mock_db.close.assert_called_once()


class TestOrchestratorDbRelease:
    def test_route_needs_embedding_semantic_only(self):
        classification = MagicMock()
        classification.expected_agents = ["semantic"]
        classification.intent = "SEMANTIC.DEFINITION.OEL"
        assert QueryOrchestrator._route_needs_embedding(classification) is True

    def test_route_needs_embedding_structured_only(self):
        classification = MagicMock()
        classification.expected_agents = ["structured"]
        classification.intent = "STRUCTURED.OEL.TWA_LOOKUP"
        assert QueryOrchestrator._route_needs_embedding(classification) is False

    def test_release_db_connection_is_safe_on_closed_session(self):
        session = MagicMock()
        session.close.side_effect = Exception("already closed")
        orch = QueryOrchestrator.__new__(QueryOrchestrator)
        orch.session = session
        orch._release_db_connection()  # should not raise


class TestEmbedBeforeDb:
    @patch("retrieval.embeddings.EmbeddingService.embed_texts")
    def test_embed_query_vector_no_session(self, mock_embed):
        dim = get_settings().vector_dimension
        mock_embed.return_value = [[0.0] * dim]
        svc = get_embedding_service()
        vector = embed_query_vector("حد TWA بنزن", embedder=svc)
        assert len(vector) == dim

    @patch("database.session.SessionLocal")
    def test_search_by_query_vector_uses_session_only_for_sql(self, mock_session_local):
        mock_db = MagicMock()
        mock_session_local.return_value = mock_db
        mock_db.execute.return_value.all.return_value = []
        dim = get_settings().vector_dimension
        vector = [0.0] * dim
        db = mock_session_local()
        results = search_by_query_vector(db, vector, limit=1)
        assert isinstance(results, list)
        db.execute.assert_called_once()
