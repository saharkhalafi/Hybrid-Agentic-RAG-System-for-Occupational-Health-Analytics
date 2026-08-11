"""Semantic Agent — production retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from retrieval.pipeline import ProductionRetrievalPipeline, RetrievalConfig, RetrievalMode


@dataclass
class SemanticAgentResult:
    success: bool
    chunks: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    retrieval_mode: str = ""
    retrieval_trace: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "chunks": self.chunks,
            "citations": self.citations,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "retrieval_mode": self.retrieval_mode,
            "retrieval_trace": self.retrieval_trace,
        }


class SemanticAgent:
    def __init__(
        self,
        session: Session | None = None,
        *,
        top_k: int = 5,
        retrieval_mode: RetrievalMode = RetrievalMode.HYBRID_RERANK,
        use_isolated_session: bool = True,
    ) -> None:
        self._parent_session = session
        self.top_k = top_k
        self.retrieval_mode = retrieval_mode
        self.use_isolated_session = use_isolated_session
        if session is not None and not use_isolated_session:
            self.pipeline = ProductionRetrievalPipeline(
                session,
                config=RetrievalConfig(
                    mode=retrieval_mode,
                    final_k=top_k,
                    candidate_k=30,
                    reranker=None,
                ),
            )
        else:
            self.pipeline = None

    def execute(self, query: str, *, page_hint: int | None = None) -> SemanticAgentResult:
        try:
            if self.use_isolated_session:
                return self._execute_isolated(query, page_hint=page_hint)
            assert self.pipeline is not None
            result = self.pipeline.retrieve(query, page_hint=page_hint)
            return self._to_result(result)
        except Exception as exc:
            return SemanticAgentResult(success=False, error=str(exc))

    def _execute_isolated(self, query: str, *, page_hint: int | None = None) -> SemanticAgentResult:
        from database.session import SessionLocal
        from persistence.semantic_store import embed_query_vector

        query_vector = embed_query_vector(query)
        db = SessionLocal()
        try:
            pipeline = ProductionRetrievalPipeline(
                db,
                config=RetrievalConfig(
                    mode=self.retrieval_mode,
                    final_k=self.top_k,
                    candidate_k=30,
                    reranker=None,
                ),
            )
            result = pipeline.retrieve(query, page_hint=page_hint, query_vector=query_vector)
            return self._to_result(result)
        finally:
            db.close()

    @staticmethod
    def _to_result(result) -> SemanticAgentResult:
        citations = [
            {
                "source_type": "semantic_text",
                "chunk_id": r.get("chunk_id"),
                "page_number": r.get("page_number"),
                "section_title": r.get("section_title"),
                "score": r.get("score"),
                "authority": "semantic",
            }
            for r in result.chunks
        ]
        return SemanticAgentResult(
            success=bool(result.chunks),
            chunks=result.chunks,
            citations=citations,
            latency_ms=result.latency_ms,
            retrieval_mode=result.mode,
            retrieval_trace=result.trace,
        )
