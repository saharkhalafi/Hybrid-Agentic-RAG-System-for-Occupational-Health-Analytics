"""Explicit Hybrid Agent execution plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HybridPlan:
    intent: str
    agents: tuple[str, ...]
    structured_intent: str | None = None
    requires_comparison: bool = False
    requires_semantic: bool = False
    description: str = ""


HYBRID_PLANS: dict[str, HybridPlan] = {
    "HYBRID.LOOKUP_AND_EXPLAIN": HybridPlan(
        intent="HYBRID.LOOKUP_AND_EXPLAIN",
        agents=("structured", "semantic"),
        structured_intent="STRUCTURED.OEL.TWA_LOOKUP",
        requires_semantic=True,
        description="Numeric OEL from PostgreSQL + semantic definition",
    ),
    "HYBRID.LOOKUP_COMPARE_EXPLAIN": HybridPlan(
        intent="HYBRID.LOOKUP_COMPARE_EXPLAIN",
        agents=("structured", "formula"),
        structured_intent="STRUCTURED.OEL.TWA_LOOKUP",
        requires_comparison=True,
        description="Structured limit + deterministic exposure comparison; semantic only if explanation requested",
    ),
    "HYBRID.LOOKUP_AND_CALCULATE": HybridPlan(
        intent="HYBRID.LOOKUP_AND_CALCULATE",
        agents=("structured", "formula"),
        structured_intent="STRUCTURED.OEL.TWA_LOOKUP",
        description="Structured lookup + formula calculation",
    ),
    "HYBRID.FORMULA_AND_EXPLAIN": HybridPlan(
        intent="HYBRID.FORMULA_AND_EXPLAIN",
        agents=("formula", "semantic"),
        description="Formula registry + semantic explanation",
    ),
    "HYBRID.MULTI_SOURCE": HybridPlan(
        intent="HYBRID.MULTI_SOURCE",
        agents=("structured", "semantic"),
        structured_intent="STRUCTURED.OEL.ALL_LIMITS_LOOKUP",
        requires_semantic=True,
        description="All limits + supporting semantic context",
    ),
}


def get_hybrid_plan(intent: str) -> HybridPlan | None:
    return HYBRID_PLANS.get(intent)


def agents_for_hybrid(intent: str, query: str, slots: dict[str, Any]) -> list[str]:
    """Select minimal required agents — do not over-invoke."""
    plan = get_hybrid_plan(intent)
    if not plan:
        return ["structured", "semantic"]
    agents = list(plan.agents)
    # Add semantic for compare only when user asks for explanation
    if plan.requires_comparison and re_explain(query):
        if "semantic" not in agents:
            agents.append("semantic")
    elif plan.requires_semantic and "semantic" not in agents:
        agents.append("semantic")
    return agents


def re_explain(query: str) -> bool:
    import re
    return bool(re.search(r"(توضیح|یعنی|چرا|مفهوم|معنا)", query))
