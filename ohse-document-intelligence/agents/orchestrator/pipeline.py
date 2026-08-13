"""Production query orchestration pipeline with domain gate, metrics, and caching."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents.formula.agent import FormulaAgent
from agents.guardrails.gate import GuardrailGate
from agents.hybrid.agent import HybridAgent
from agents.orchestrator.trace import QueryTrace, new_trace_id
from agents.routing.classifier import IntentClassifier
from agents.routing.query_understanding import enhance_normalization, understand_query
from agents.routing.router import QueryRouter
from agents.semantic.agent import SemanticAgent
from agents.session.context import SessionContextManager
from agents.session.store import SessionStore
from agents.structured.agent import StructuredAgent
from agents.structured.store import PostgresStructuredStore
from agents.synthesis.answer import AnswerSynthesizer
from config.logging import get_logger
from config.settings import get_settings
from observability.cost_model import compute_request_cost
from observability.embed_stats import get_embed_stats, reset_embed_stats
from observability.metrics import get_metrics
from security.domain_gate import DomainSafetyGate, GateDecision
from sqlalchemy.exc import OperationalError

logger = get_logger(__name__)

# Shared session store across requests (process-level singleton)
_shared_session_store: SessionStore | None = None


def get_shared_session_store() -> SessionStore:
    global _shared_session_store
    if _shared_session_store is None:
        settings = get_settings()
        _shared_session_store = SessionStore(
            ttl_seconds=settings.session_ttl_seconds,
            max_turns=settings.session_max_turns,
        )
    return _shared_session_store


@dataclass
class QueryResponse:
    answer: str
    intent: str
    agents: list[str]
    citations: list[dict[str, Any]]
    confidence: float
    trace_id: str
    session_id: str | None = None
    requires_clarification: bool = False
    gate_decision: str = "pass"
    success: bool = True
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "success": self.success,
            "answer": self.answer,
            "intent": self.intent,
            "agents": self.agents,
            "citations": self.citations,
            "confidence": self.confidence,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "requires_clarification": self.requires_clarification,
            "gate_decision": self.gate_decision,
            "metadata": self.metadata,
        }
        if self.error is not None:
            out["error"] = self.error
        return out


class QueryOrchestrator:
    def __init__(
        self,
        session: Session,
        *,
        session_store: SessionStore | None = None,
        domain_gate: DomainSafetyGate | None = None,
    ) -> None:
        self.session = session
        store = PostgresStructuredStore(session)
        self.structured_agent = StructuredAgent(store)
        self.semantic_agent = SemanticAgent(use_isolated_session=True)
        self.formula_agent = FormulaAgent(session)
        self.hybrid_agent = HybridAgent(self.structured_agent, self.semantic_agent, self.formula_agent)
        self.router = QueryRouter(
            self.structured_agent, self.semantic_agent, self.formula_agent, self.hybrid_agent
        )
        self.classifier = IntentClassifier()
        self.context_mgr = SessionContextManager(session_store or get_shared_session_store())
        self.guardrails = GuardrailGate()
        self.synthesizer = AnswerSynthesizer()
        settings = get_settings()
        self.domain_gate = domain_gate or DomainSafetyGate()
        self.gate_enabled = settings.domain_gate_enabled
        self.metrics = get_metrics()

    def _release_db_connection(self) -> None:
        """Return the request-scoped connection to the pool during external I/O."""
        try:
            if self.session is not None:
                self.session.close()
        except Exception:
            pass

    def _ensure_db_connection(self) -> None:
        """Re-acquire a DB session after external I/O if needed."""
        from database.session import SessionLocal

        if self.session is None or not self.session.is_active:
            self.session = SessionLocal()
            store = PostgresStructuredStore(self.session)
            self.structured_agent = StructuredAgent(store)
            self.formula_agent = FormulaAgent(self.session)
            self.hybrid_agent = HybridAgent(self.structured_agent, self.semantic_agent, self.formula_agent)
            self.router = QueryRouter(
                self.structured_agent, self.semantic_agent, self.formula_agent, self.hybrid_agent
            )

    @staticmethod
    def _route_needs_embedding(classification) -> bool:
        agents = list(classification.expected_agents or [])
        if not agents:
            meta_agent = classification.intent.split(".")[0].lower() if classification.intent else ""
            if meta_agent in ("semantic", "hybrid"):
                return True
        return "semantic" in agents or "hybrid" in agents or (
            len(agents) == 1 and agents[0] == "semantic"
        )

    def handle(self, query: str, *, session_id: str | None = None) -> QueryResponse:
        t0 = time.perf_counter()
        reset_embed_stats()
        trace = QueryTrace(trace_id=new_trace_id(), session_id=session_id, raw_query=query)
        self.metrics.inc("queries_total")

        # ── Phase D: Domain/Safety Gate (before everything else) ──────────
        session_ctx: dict[str, Any] | None = None
        if session_id:
            existing = self.context_mgr.store.get(session_id)
            if existing:
                session_ctx = existing.to_gate_context()

        if self.gate_enabled:
            gate = self.domain_gate.evaluate(query, session_context=session_ctx)
            trace.context_trace.append(f"gate:{gate.decision.value}")
            self.metrics.inc(f"gate_{gate.decision.value}")

            if gate.decision == GateDecision.BLOCK:
                self.metrics.inc("queries_blocked")
                return self._gate_response(
                    gate, trace, session_id, t0,
                    intent="GUARDRAIL.BLOCKED",
                )
            if gate.decision == GateDecision.REJECT:
                self.metrics.inc("queries_rejected")
                return self._gate_response(
                    gate, trace, session_id, t0,
                    intent="DOMAIN.REJECTED",
                )
            gate_decision_str = gate.decision.value
        else:
            gate_decision_str = "pass"

        normalized = enhance_normalization(query)
        state, ctx = self.context_mgr.begin_turn(session_id, normalized)
        trace.resolved_query = ctx.resolved_query
        trace.turn_id = ctx.turn_id
        trace.context_trace.extend(ctx.context_trace)

        try:
            understanding = understand_query(
                self.session,
                ctx.resolved_query,
                inherited_slots=ctx.merged_slots,
                raw_query=query,
            )
        except OperationalError as exc:
            logger.error("database_unavailable", trace_id=trace.trace_id, error=str(exc))
            return self._system_error_response(
                trace,
                session_id,
                t0,
                code="DATABASE_UNAVAILABLE",
                message="Database is unavailable.",
                message_fa="پایگاه داده در دسترس نیست. لطفاً بعداً دوباره تلاش کنید.",
                gate_decision=gate_decision_str,
            )
        trace.context_trace.extend(understanding.trace)

        slots = understanding.slots
        if understanding.requires_clarification and understanding.clarification_reason:
            ctx.ambiguous = True
            if "chemical" in (understanding.clarification_reason or ""):
                ctx.missing_for_resolution.append("chemical_name")
        elif slots.get("chemical_name") or slots.get("cas"):
            ctx.ambiguous = False
            ctx.missing_for_resolution = [
                m for m in ctx.missing_for_resolution if m not in ("chemical_name", "cas")
            ]
        trace.slots = slots

        classification = self.classifier.classify(
            ctx.resolved_query,
            slots=slots,
            session_ambiguous=ctx.ambiguous,
            missing_slots=ctx.missing_for_resolution,
        )
        trace.intent = classification.intent
        trace.intent_confidence = classification.confidence
        trace.selected_agents = classification.expected_agents

        # Cost optimization: structured/formula/guardrail intents skip embedding
        planned = self.router.plan(classification)
        if planned == ["semantic"]:
            self._release_db_connection()

        agent_results = self.router.execute(classification, ctx.resolved_query, slots)
        self._ensure_db_connection()
        trace.agent_execution_order = agent_results.get("planned_agents", [])
        self._fill_trace_latencies(trace, agent_results)

        guard = self.guardrails.evaluate(
            intent=classification.intent,
            classification={
                "requires_clarification": classification.requires_clarification,
                "numeric_safety_level": classification.numeric_safety_level,
                "ambiguous_chemical": slots.get("ambiguous_chemical"),
            },
            agent_results=agent_results,
        )
        trace.guardrail_decision = guard.action
        trace.validation_result = "pass" if guard.allowed else guard.action
        if guard.action != "pass":
            self.metrics.inc(f"guardrail_{guard.action}")

        answer, citations = self.synthesizer.synthesize(
            intent=classification.intent,
            query=ctx.resolved_query,
            agent_results=agent_results,
            guardrail_message=guard.message_fa if not guard.allowed else None,
        )
        trace.final_answer = answer
        trace.citations = citations
        trace.total_latency_ms = (time.perf_counter() - t0) * 1000

        self.context_mgr.commit_turn(
            state,
            ctx=ctx,
            intent=classification.intent,
            slots=slots,
            trace_id=trace.trace_id,
            agent_results=agent_results,
        )

        self.metrics.observe("query_latency_ms", trace.total_latency_ms)
        for agent_name, lat in trace.agent_latency.items():
            self.metrics.observe(f"agent_{agent_name}_latency_ms", lat)

        logger.info(
            "query_completed",
            trace_id=trace.trace_id,
            intent=classification.intent,
            agents=trace.agent_execution_order,
            latency_ms=round(trace.total_latency_ms, 1),
            gate="pass",
        )

        gate_decision = gate_decision_str

        cost = compute_request_cost(
            intent=classification.intent,
            agents=trace.agent_execution_order,
            gate_decision=gate_decision,
            session_id=session_id,
            turn_id=trace.turn_id,
            latency_ms=trace.total_latency_ms,
            agent_latency=trace.agent_latency,
            embed_stats=get_embed_stats(),
        )

        return QueryResponse(
            answer=answer,
            intent=classification.intent,
            agents=trace.agent_execution_order,
            citations=citations,
            confidence=classification.confidence,
            trace_id=trace.trace_id,
            session_id=session_id,
            requires_clarification=classification.requires_clarification or guard.action == "clarify",
            gate_decision=gate_decision,
            metadata={"trace": trace.to_dict(), "cost": cost.to_dict()},
        )

    def _gate_response(
        self,
        gate,
        trace: QueryTrace,
        session_id: str | None,
        t0: float,
        *,
        intent: str,
    ) -> QueryResponse:
        trace.total_latency_ms = (time.perf_counter() - t0) * 1000
        trace.guardrail_decision = gate.decision.value
        trace.validation_result = gate.decision.value
        trace.final_answer = gate.message_fa or "درخواست رد شد."
        self.metrics.observe("query_latency_ms", trace.total_latency_ms)
        logger.info(
            "query_gated",
            trace_id=trace.trace_id,
            decision=gate.decision.value,
            reasons=gate.reasons,
            latency_ms=round(trace.total_latency_ms, 1),
        )
        cost = compute_request_cost(
            intent=intent,
            agents=[],
            gate_decision=gate.decision.value,
            session_id=session_id,
            turn_id=trace.turn_id,
            latency_ms=trace.total_latency_ms,
            agent_latency={},
            embed_stats=get_embed_stats(),
        )
        return QueryResponse(
            answer=gate.message_fa or "درخواست رد شد.",
            intent=intent,
            agents=[],
            citations=[],
            confidence=gate.confidence,
            trace_id=trace.trace_id,
            session_id=session_id,
            requires_clarification=gate.decision == GateDecision.CLARIFY,
            gate_decision=gate.decision.value,
            success=False,
            error={
                "code": f"GATE_{gate.decision.value.upper()}",
                "message": gate.message_fa or "Request blocked by domain gate",
                "message_fa": gate.message_fa,
                "details": {"reasons": gate.reasons, "signals": gate.signals},
            },
            metadata={"trace": trace.to_dict(), "gate": gate.signals, "cost": cost.to_dict()},
        )

    def _system_error_response(
        self,
        trace: QueryTrace,
        session_id: str | None,
        t0: float,
        *,
        code: str,
        message: str,
        message_fa: str,
        gate_decision: str = "pass",
    ) -> QueryResponse:
        trace.total_latency_ms = (time.perf_counter() - t0) * 1000
        trace.guardrail_decision = "error"
        trace.validation_result = "error"
        trace.final_answer = message_fa
        self.metrics.observe("query_latency_ms", trace.total_latency_ms)
        self.metrics.inc("queries_failed")
        return QueryResponse(
            answer=message_fa,
            intent="SYSTEM.ERROR",
            agents=[],
            citations=[],
            confidence=0.0,
            trace_id=trace.trace_id,
            session_id=session_id,
            gate_decision=gate_decision,
            success=False,
            error={
                "code": code,
                "message": message,
                "message_fa": message_fa,
                "details": {},
            },
            metadata={"trace": trace.to_dict()},
        )

    @staticmethod
    def _fill_trace_latencies(trace: QueryTrace, agent_results: dict[str, Any]) -> None:
        for key in ("structured", "semantic", "formula", "hybrid"):
            block = agent_results.get(key)
            if isinstance(block, dict) and "latency_ms" in block:
                trace.agent_latency[key] = block["latency_ms"]
        sem = agent_results.get("semantic") or (agent_results.get("hybrid") or {}).get("agent_results", {}).get("semantic")
        if isinstance(sem, dict):
            chunks = sem.get("chunks") or []
            trace.retrieval_scores = [c.get("score", 0) for c in chunks if c.get("score") is not None]
            trace.source_ids = [c.get("chunk_id") for c in chunks if c.get("chunk_id")]
