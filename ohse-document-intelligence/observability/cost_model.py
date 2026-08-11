"""Per-request cost estimation (GCP list-price approximations, configurable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Vertex AI / Cloud SQL list-price estimates (USD, March 2026 baseline).
# gemini-embedding-001: ~$0.000025 / 1K input chars (batch pricing varies).
# Cloud SQL: amortized ~$0.10/vCPU-hour → ~$0.00003 per 100ms query slice.
PRICING_USD = {
    "domain_gate_per_request": 0.000001,       # pure CPU, negligible
    "postgres_per_ms": 0.0000003,              # amortized Cloud SQL compute
    "embedding_per_1k_chars": 0.000025,        # Vertex AI gemini-embedding-001
    "retrieval_per_query": 0.000002,           # pgvector search overhead
    "llm_per_1k_output_tokens": 0.0,           # template synthesis — no LLM
    "cache_hit_embedding_usd": 0.0,            # zero marginal cost
}


@dataclass
class RequestCost:
    category: str = "unknown"
    gate_decision: str = "pass"
    gate_usd: float = 0.0
    embedding_usd: float = 0.0
    embedding_cache_hit: bool = False
    embedding_chars: int = 0
    postgres_usd: float = 0.0
    vertex_ai_usd: float = 0.0
    llm_usd: float = 0.0
    cache_usd: float = 0.0
    retrieval_usd: float = 0.0
    total_usd: float = 0.0
    latency_ms: float = 0.0
    agents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "gate_decision": self.gate_decision,
            "gate_usd": round(self.gate_usd, 8),
            "embedding_usd": round(self.embedding_usd, 8),
            "embedding_cache_hit": self.embedding_cache_hit,
            "embedding_chars": self.embedding_chars,
            "postgres_usd": round(self.postgres_usd, 8),
            "vertex_ai_usd": round(self.vertex_ai_usd, 8),
            "llm_usd": round(self.llm_usd, 8),
            "cache_usd": round(self.cache_usd, 8),
            "retrieval_usd": round(self.retrieval_usd, 8),
            "total_usd": round(self.total_usd, 8),
            "latency_ms": round(self.latency_ms, 2),
            "agents": self.agents,
        }


def categorize_query(
    *,
    intent: str,
    agents: list[str],
    gate_decision: str,
    session_id: str | None,
    turn_id: int,
    embedding_cache_hit: bool,
) -> str:
    if gate_decision in ("reject", "block"):
        return "Rejected"
    if embedding_cache_hit:
        return "Cached"
    if session_id and turn_id > 1:
        return "Follow-up"
    if intent.startswith("HYBRID.") or len(agents) > 1:
        return "Hybrid"
    if intent.startswith("FORMULA.") or "formula" in agents:
        return "Formula"
    if intent.startswith("STRUCTURED.") or "structured" in agents:
        return "Structured"
    if intent.startswith("SEMANTIC.") or "semantic" in agents:
        return "Semantic"
    if gate_decision == "clarify":
        return "Structured"
    return "Other"


def compute_request_cost(
    *,
    intent: str,
    agents: list[str],
    gate_decision: str,
    session_id: str | None,
    turn_id: int,
    latency_ms: float,
    agent_latency: dict[str, float],
    embed_stats: dict[str, int],
) -> RequestCost:
    hits = embed_stats.get("hits", 0)
    misses = embed_stats.get("misses", 0)
    chars = embed_stats.get("chars", 0)
    cache_hit = hits > 0 and misses == 0

    cat = categorize_query(
        intent=intent,
        agents=agents,
        gate_decision=gate_decision,
        session_id=session_id,
        turn_id=turn_id,
        embedding_cache_hit=cache_hit,
    )

    gate_usd = PRICING_USD["domain_gate_per_request"]
    if gate_decision in ("reject", "block"):
        return RequestCost(
            category="Rejected",
            gate_decision=gate_decision,
            gate_usd=gate_usd,
            total_usd=gate_usd,
            latency_ms=latency_ms,
            agents=agents,
        )

    postgres_ms = sum(agent_latency.values()) if agent_latency else 0.0
    if postgres_ms <= 0:
        # Gate-only / rejected: minimal DB; semantic wall-clock includes embed wait
        if cat in ("Semantic", "Hybrid", "Cached"):
            postgres_ms = 150.0  # typical pgvector + ORM slice
        elif cat in ("Structured", "Formula", "Follow-up"):
            postgres_ms = min(latency_ms, 50.0)
        else:
            postgres_ms = min(latency_ms * 0.05, 30.0)
    postgres_usd = postgres_ms * PRICING_USD["postgres_per_ms"]

    if cache_hit:
        embedding_usd = PRICING_USD["cache_hit_embedding_usd"]
    elif misses > 0:
        embedding_usd = (chars / 1000.0) * PRICING_USD["embedding_per_1k_chars"]
    else:
        embedding_usd = 0.0

    retrieval_usd = 0.0
    if "semantic" in agents or "hybrid" in agents:
        retrieval_usd = PRICING_USD["retrieval_per_query"]

    llm_usd = 0.0  # template synthesis — no generative LLM
    vertex_usd = embedding_usd
    cache_usd = PRICING_USD["cache_hit_embedding_usd"] if cache_hit else 0.0

    total = gate_usd + postgres_usd + embedding_usd + retrieval_usd + llm_usd

    return RequestCost(
        category=cat,
        gate_decision=gate_decision,
        gate_usd=gate_usd,
        embedding_usd=embedding_usd,
        embedding_cache_hit=cache_hit,
        embedding_chars=chars,
        postgres_usd=postgres_usd,
        vertex_ai_usd=vertex_usd,
        llm_usd=llm_usd,
        cache_usd=cache_usd,
        retrieval_usd=retrieval_usd,
        total_usd=total,
        latency_ms=latency_ms,
        agents=agents,
    )


def aggregate_costs(costs: list[RequestCost]) -> dict[str, Any]:
    if not costs:
        return {}
    by_cat: dict[str, list[RequestCost]] = {}
    for c in costs:
        by_cat.setdefault(c.category, []).append(c)

    per_category = {}
    for cat, items in sorted(by_cat.items()):
        n = len(items)
        per_category[cat] = {
            "count": n,
            "avg_total_usd": round(sum(i.total_usd for i in items) / n, 8),
            "avg_embedding_usd": round(sum(i.embedding_usd for i in items) / n, 8),
            "avg_postgres_usd": round(sum(i.postgres_usd for i in items) / n, 8),
            "avg_retrieval_usd": round(sum(i.retrieval_usd for i in items) / n, 8),
            "cache_hit_rate": round(sum(1 for i in items if i.embedding_cache_hit) / n, 4),
            "avg_latency_ms": round(sum(i.latency_ms for i in items) / n, 2),
        }

    total_usd = sum(c.total_usd for c in costs)
    n = len(costs)
    return {
        "total_requests": n,
        "total_usd": round(total_usd, 6),
        "avg_usd_per_request": round(total_usd / n, 8),
        "avg_usd_per_1000": round(total_usd / n * 1000, 4),
        "per_category": per_category,
        "breakdown_usd": {
            "gate": round(sum(c.gate_usd for c in costs), 6),
            "embedding": round(sum(c.embedding_usd for c in costs), 6),
            "postgres": round(sum(c.postgres_usd for c in costs), 6),
            "vertex_ai": round(sum(c.vertex_ai_usd for c in costs), 6),
            "llm": round(sum(c.llm_usd for c in costs), 6),
            "retrieval": round(sum(c.retrieval_usd for c in costs), 6),
        },
    }
