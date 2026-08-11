"""Hybrid Agent — orchestrates structured, semantic, and formula agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.formula.agent import FormulaAgent
from agents.hybrid.plans import agents_for_hybrid, get_hybrid_plan
from agents.semantic.agent import SemanticAgent
from agents.structured.agent import StructuredAgent


@dataclass
class HybridAgentResult:
    success: bool
    agent_results: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "agent_results": self.agent_results,
            "citations": self.citations,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


class HybridAgent:
    def __init__(
        self,
        structured: StructuredAgent,
        semantic: SemanticAgent,
        formula: FormulaAgent,
    ) -> None:
        self.structured = structured
        self.semantic = semantic
        self.formula = formula

    def execute(
        self,
        intent: str,
        query: str,
        slots: dict[str, Any],
        *,
        agents: list[str],
    ) -> HybridAgentResult:
        import time

        t0 = time.perf_counter()
        results: dict[str, Any] = {}
        citations: list[dict[str, Any]] = []
        plan = get_hybrid_plan(intent)
        if plan and not agents:
            agents = list(plan.agents)
        elif plan is None and agents:
            agents = agents_for_hybrid(intent, query, slots)
        structured_intents = {
            "HYBRID.LOOKUP_AND_EXPLAIN": plan.structured_intent if plan else "STRUCTURED.OEL.TWA_LOOKUP",
            "HYBRID.LOOKUP_AND_CALCULATE": plan.structured_intent if plan else "STRUCTURED.OEL.TWA_LOOKUP",
            "HYBRID.LOOKUP_COMPARE_EXPLAIN": plan.structured_intent if plan else "STRUCTURED.OEL.TWA_LOOKUP",
            "HYBRID.MULTI_SOURCE": plan.structured_intent if plan else "STRUCTURED.OEL.ALL_LIMITS_LOOKUP",
        }

        if "structured" in agents:
            s_intent = structured_intents.get(intent, "STRUCTURED.OEL.TWA_LOOKUP")
            s_res = self.structured.execute(s_intent, slots)
            results["structured"] = s_res.to_dict()
            citations.extend(s_res.citations)

        if "formula" in agents:
            if slots.get("variables"):
                f_res = self.formula.execute("FORMULA.CALCULATION.VIBRATION_AHV", slots)
            else:
                f_res = self.formula.execute("FORMULA.LOOKUP.BY_ID", slots)
            results["formula"] = f_res.to_dict()
            citations.extend(f_res.citations)

        if "semantic" in agents:
            parent_sess = getattr(getattr(self.structured, "store", None), "session", None)
            if parent_sess is not None:
                try:
                    parent_sess.close()
                except Exception:
                    pass
            sem_res = self.semantic.execute(query)
            results["semantic"] = sem_res.to_dict()
            citations.extend(sem_res.citations)

        results["execution_plan"] = {
            "intent": intent,
            "agents": agents,
            "plan": plan.description if plan else "default",
        }

        # comparison if exposure + structured data
        if intent == "HYBRID.LOOKUP_COMPARE_EXPLAIN" and results.get("structured", {}).get("success"):
            sdata = results["structured"]["data"]
            conc = slots.get("concentration")
            twa = sdata.get("value") or sdata.get("twa")
            if conc is not None and twa is not None:
                try:
                    ratio = float(conc) / float(twa)
                    results["comparison"] = {
                        "concentration": conc,
                        "limit": twa,
                        "unit": sdata.get("unit"),
                        "ratio": ratio,
                        "exceeds": ratio > 1.0,
                        "authority": "formula_engine",
                    }
                except (TypeError, ZeroDivisionError):
                    pass

        success = any(r.get("success") for r in results.values() if isinstance(r, dict))
        return HybridAgentResult(
            success=success,
            agent_results=results,
            citations=citations,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
