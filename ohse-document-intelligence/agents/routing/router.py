"""Query Router — plans and executes agents per intent taxonomy."""

from __future__ import annotations

from typing import Any

from agents.formula.agent import FormulaAgent
from agents.hybrid.agent import HybridAgent
from agents.routing.classifier import ClassificationResult
from agents.semantic.agent import SemanticAgent
from agents.structured.agent import StructuredAgent
from intent.taxonomy_schema import load_taxonomy


class QueryRouter:
    def __init__(
        self,
        structured: StructuredAgent,
        semantic: SemanticAgent,
        formula: FormulaAgent,
        hybrid: HybridAgent,
    ) -> None:
        self.structured = structured
        self.semantic = semantic
        self.formula = formula
        self.hybrid = hybrid
        self.taxonomy = load_taxonomy().by_id()

    def plan(self, classification: ClassificationResult) -> list[str]:
        meta = self.taxonomy.get(classification.intent)
        if meta and meta.hybrid_agents:
            return list(meta.hybrid_agents)
        if classification.expected_agents:
            return list(classification.expected_agents)
        if meta:
            return [meta.agent]
        return ["semantic"]

    def execute(
        self,
        classification: ClassificationResult,
        query: str,
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        intent = classification.intent
        agents = self.plan(classification)
        results: dict[str, Any] = {"planned_agents": agents}

        meta = self.taxonomy.get(intent)
        agent_name = meta.agent if meta else "semantic"

        if agent_name == "clarify" or agent_name == "guardrail":
            results["guardrail"] = {"intent": intent, "requires_clarification": classification.requires_clarification}
            return results

        if agent_name == "hybrid" or len(agents) > 1:
            h = self.hybrid.execute(intent, query, slots, agents=agents)
            results["hybrid"] = h.to_dict()
            return results

        if agent_name == "structured":
            s = self.structured.execute(intent, slots)
            results["structured"] = s.to_dict()
            return results

        if agent_name == "formula":
            f = self.formula.execute(intent, slots)
            results["formula"] = f.to_dict()
            return results

        sem = self.semantic.execute(query)
        results["semantic"] = sem.to_dict()
        return results
