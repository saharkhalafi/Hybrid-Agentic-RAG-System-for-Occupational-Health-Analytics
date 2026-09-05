"""Semantic Agent — production retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from retrieval.pipeline import ProductionRetrievalPipeline, RetrievalConfig, RetrievalMode

# Production default: multi-source candidates before identity-aware rerank.
DEFAULT_RETRIEVAL_MODE = RetrievalMode.EVIDENCE_CANDIDATES
# A/B canary experimental arm only — not the production default until canary confirms significance.
CANARY_EXPERIMENTAL_MODE = RetrievalMode.HYBRID_RERANK
# Fallback for the HYBRID canary arm when primary errors or returns empty with page_hint.
HYBRID_CANARY_FALLBACK_MODE = RetrievalMode.VECTOR_METADATA


@dataclass
class SemanticAgentResult:
    success: bool
    chunks: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    retrieval_mode: str = ""
    retrieval_trace: list[str] = field(default_factory=list)
    pre_rerank_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "chunks": self.chunks,
            "citations": self.citations,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "retrieval_mode": self.retrieval_mode,
            "retrieval_trace": self.retrieval_trace,
            "pre_rerank_candidates": self.pre_rerank_candidates,
        }


class SemanticAgent:
    def __init__(
        self,
        session: Session | None = None,
        *,
        top_k: int = 5,
        retrieval_mode: RetrievalMode = DEFAULT_RETRIEVAL_MODE,
        fallback_mode: RetrievalMode | None = None,
        use_isolated_session: bool = True,
    ) -> None:
        self._parent_session = session
        self.top_k = top_k
        self.retrieval_mode = retrieval_mode
        self.fallback_mode = fallback_mode
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

    def execute(
        self,
        query: str,
        *,
        page_hint: int | None = None,
        slots: dict[str, Any] | None = None,
    ) -> SemanticAgentResult:
        try:
            if self.use_isolated_session:
                return self._execute_isolated(query, page_hint=page_hint, slots=slots)
            assert self.pipeline is not None
            return self._retrieve_with_fallback(
                self.pipeline, query, page_hint=page_hint, slots=slots
            )
        except Exception as exc:
            return SemanticAgentResult(success=False, error=str(exc))

    def _execute_isolated(
        self,
        query: str,
        *,
        page_hint: int | None = None,
        slots: dict[str, Any] | None = None,
    ) -> SemanticAgentResult:
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
            return self._retrieve_with_fallback(
                pipeline,
                query,
                page_hint=page_hint,
                query_vector=query_vector,
                slots=slots,
            )
        finally:
            db.close()

    def _retrieve_with_fallback(
        self,
        pipeline: ProductionRetrievalPipeline,
        query: str,
        *,
        page_hint: int | None = None,
        query_vector: list[float] | None = None,
        slots: dict[str, Any] | None = None,
    ) -> SemanticAgentResult:
        """Optional fallback when primary mode errors or returns empty with page_hint.

        Production default (VECTOR_METADATA) does not enable fallback. HYBRID_RERANK
        canary arm may set fallback_mode=VECTOR_METADATA explicitly.
        """
        if self.fallback_mode is None or self.fallback_mode == self.retrieval_mode:
            result = pipeline.retrieve(
                query, page_hint=page_hint, query_vector=query_vector, slots=slots
            )
            return self._to_result(result)

        fallback_reason: str | None = None
        try:
            result = pipeline.retrieve(
                query, page_hint=page_hint, query_vector=query_vector, slots=slots
            )
        except Exception as exc:
            if self.retrieval_mode == self.fallback_mode:
                raise
            fallback_reason = f"primary_error:{type(exc).__name__}"
            result = None
        else:
            if (
                not result.chunks
                and page_hint is not None
                and self.retrieval_mode != self.fallback_mode
            ):
                fallback_reason = "empty_with_page_hint"

        if fallback_reason:
            fallback_pipeline = ProductionRetrievalPipeline(
                pipeline.session,
                config=RetrievalConfig(
                    mode=self.fallback_mode,
                    final_k=self.top_k,
                    candidate_k=30,
                    reranker=None,
                ),
            )
            result = fallback_pipeline.retrieve(
                query,
                page_hint=page_hint,
                query_vector=query_vector,
                slots=slots,
            )
            agent_result = self._to_result(result)
            agent_result.retrieval_trace = [
                f"fallback:{fallback_reason}",
                f"fallback_mode:{self.fallback_mode.value}",
                *agent_result.retrieval_trace,
            ]
            return agent_result

        return self._to_result(result)

    @staticmethod
    def _to_result(result) -> SemanticAgentResult:
        citations = [
            {
                "source_type": "semantic_text",
                "chunk_id": r.get("chunk_id"),
                "page_number": r.get("document_page")
                if r.get("document_page") is not None
                else r.get("page_number"),
                "printed_page_number": r.get("printed_page_number"),
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
            pre_rerank_candidates=list(getattr(result, "pre_rerank_candidates", []) or []),
        )
