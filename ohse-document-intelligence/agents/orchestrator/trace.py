"""Execution trace for observability."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryTrace:
    trace_id: str
    session_id: str | None = None
    turn_id: int = 1
    raw_query: str = ""
    resolved_query: str = ""
    intent: str = ""
    intent_confidence: float = 0.0
    slots: dict[str, Any] = field(default_factory=dict)
    selected_agents: list[str] = field(default_factory=list)
    agent_execution_order: list[str] = field(default_factory=list)
    agent_latency: dict[str, float] = field(default_factory=dict)
    retrieval_scores: list[float] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    formula_id: str | None = None
    validation_result: str = ""
    guardrail_decision: str = ""
    no_data_reason: str | None = None
    final_answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    context_trace: list[str] = field(default_factory=list)
    total_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "raw_query": self.raw_query,
            "resolved_query": self.resolved_query,
            "intent": self.intent,
            "intent_confidence": self.intent_confidence,
            "slots": self.slots,
            "selected_agents": self.selected_agents,
            "agent_execution_order": self.agent_execution_order,
            "agent_latency": self.agent_latency,
            "retrieval_scores": self.retrieval_scores,
            "source_ids": self.source_ids,
            "formula_id": self.formula_id,
            "validation_result": self.validation_result,
            "guardrail_decision": self.guardrail_decision,
            "no_data_reason": self.no_data_reason,
            "final_answer": self.final_answer,
            "citations": self.citations,
            "context_trace": self.context_trace,
            "total_latency_ms": self.total_latency_ms,
        }


def new_trace_id() -> str:
    return str(uuid.uuid4())
