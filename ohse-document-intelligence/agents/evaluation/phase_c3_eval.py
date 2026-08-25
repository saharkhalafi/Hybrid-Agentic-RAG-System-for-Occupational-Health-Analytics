"""Phase C.3 evaluation harness — retrieval modes, router, agents."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT
from retrieval.pipeline import ProductionRetrievalPipeline, RetrievalConfig, RetrievalMode
from retrieval.reranker import LexicalReranker, NoOpReranker, get_available_rerankers
from retrieval.semantic_baseline_eval import evaluate_semantic_retrieval, load_semantic_eval_cases
from agents.evaluation.harness import evaluate_router, evaluate_by_category, resolve_master_path

OUT_DIR = PROJECT_ROOT / "data" / "evaluation" / "phase_c3_results"
BASELINE_DIR = PROJECT_ROOT / "data" / "evaluation"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]

# Phase C.2 baseline (from reports)
PHASE_C2_BASELINE = {
    "router_intent_accuracy": 0.7572,
    "router_agent_accuracy": 0.9067,
    "formula_intent_accuracy": 0.7232,
    "conversational_intent_accuracy": 0.4270,
    "semantic_recall_at_5_vector": 0.267,
    "semantic_recall_at_5_metadata_filter": 0.433,
    "structured_routing": 0.9366,
}


@dataclass
class PhaseC3Summary:
    retrieval_modes: dict[str, dict[str, float]] = field(default_factory=dict)
    router: dict[str, Any] = field(default_factory=dict)
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)
    rerankers: list[dict[str, Any]] = field(default_factory=list)
    acceptance: dict[str, dict[str, Any]] = field(default_factory=dict)


def benchmark_retrieval_modes(session, *, limit: int | None = None) -> dict[str, dict[str, float]]:
    """Benchmark retrieval architecture options A–D."""
    modes = {
        "A_vector_only": RetrievalMode.VECTOR_ONLY,
        "B_vector_lexical": RetrievalMode.VECTOR_LEXICAL,
        "C_vector_metadata": RetrievalMode.VECTOR_METADATA,
        "D_hybrid_rerank": RetrievalMode.HYBRID_RERANK,
    }
    results: dict[str, dict[str, float]] = {}
    cases = load_semantic_eval_cases(limit)
    if not cases:
        return results

    for label, mode in modes.items():
        reranker = LexicalReranker() if mode in {RetrievalMode.VECTOR_LEXICAL, RetrievalMode.HYBRID_RERANK} else NoOpReranker()
        cfg = RetrievalConfig(mode=mode, candidate_k=30, final_k=5, reranker=reranker)
        pipeline = ProductionRetrievalPipeline(session, config=cfg)
        from retrieval.eval_metrics import recall_at_k, mrr, ndcg_at_k, precision_at_k
        from persistence.semantic_store import embed_query_vector

        query_vectors: dict[str, list[float]] = {}
        for case in cases:
            if case.query not in query_vectors:
                query_vectors[case.query] = embed_query_vector(case.query)

        rows = []
        latencies = []
        for case in cases:
            t0 = time.perf_counter()
            res = pipeline.retrieve(
                case.query,
                page_hint=case.page_number,
                query_vector=query_vectors[case.query],
            )
            latencies.append((time.perf_counter() - t0) * 1000)
            retrieved = [c.get("chunk_id", "") for c in res.chunks]
            rows.append({"retrieved": retrieved, "relevant": case.relevant_chunk_ids, "graded": case.graded})

        n = len(rows)
        results[label] = {
            "recall_at_1": sum(recall_at_k(r["retrieved"], r["relevant"], 1) for r in rows) / n if n else 0,
            "recall_at_3": sum(recall_at_k(r["retrieved"], r["relevant"], 3) for r in rows) / n if n else 0,
            "recall_at_5": sum(recall_at_k(r["retrieved"], r["relevant"], 5) for r in rows) / n if n else 0,
            "recall_at_10": sum(recall_at_k(r["retrieved"], r["relevant"], 10) for r in rows) / n if n else 0,
            "mrr": sum(mrr(r["retrieved"], r["relevant"]) for r in rows) / n if n else 0,
            "ndcg_at_5": sum(ndcg_at_k(r["retrieved"], r["graded"], 5) for r in rows) / n if n else 0,
            "ndcg_at_10": sum(ndcg_at_k(r["retrieved"], r["graded"], 10) for r in rows) / n if n else 0,
            "precision_at_5": sum(precision_at_k(r["retrieved"], r["relevant"], 5) for r in rows) / n if n else 0,
            "avg_latency_ms": sum(latencies) / len(latencies) if latencies else 0,
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "n_queries": n,
        }
    return results


def benchmark_rerankers_local(session, *, limit: int = 50) -> list[dict[str, Any]]:
    """Benchmark all locally available rerankers."""
    results = []
    for reranker in get_available_rerankers():
        for ck in [10, 20, 30]:
            r = evaluate_semantic_retrieval(
                session,
                reranker=reranker,
                candidate_k=ck,
                final_k=5,
                limit=limit,
            )
            info = reranker.info()
            results.append({
                "reranker": r.reranker_name,
                "candidate_k": ck,
                "recall_at_5": r.recall_at_5,
                "recall_at_10": r.recall_at_10,
                "mrr": r.mrr,
                "ndcg_at_5": r.ndcg_at_5,
                "precision_at_5": r.precision_at_5,
                "latency_ms": r.avg_latency_ms,
                "model_size_mb": info.model_size_mb,
                "installation": info.installation,
                "n": r.total_queries,
            })
    return results


def check_acceptance(summary: PhaseC3Summary) -> dict[str, dict[str, Any]]:
    router = summary.router
    cats = summary.per_category
    best_retrieval = max(summary.retrieval_modes.items(), key=lambda x: x[1].get("recall_at_5", 0), default=("", {}))

    return {
        "structured_routing": {
            "target": 0.95,
            "actual": cats.get("structured", {}).get("intent_accuracy", 0),
            "passed": cats.get("structured", {}).get("intent_accuracy", 0) >= 0.95,
        },
        "conversational_context": {
            "target": 0.90,
            "actual": router.get("context_resolution_accuracy", 0),
            "passed": router.get("context_resolution_accuracy", 0) >= 0.90,
        },
        "formula_routing": {
            "target": 0.95,
            "actual": cats.get("formula", {}).get("intent_accuracy", 0),
            "passed": cats.get("formula", {}).get("intent_accuracy", 0) >= 0.95,
        },
        "semantic_recall_at_5": {
            "target": 0.80,
            "actual": best_retrieval[1].get("recall_at_5", 0) if best_retrieval else 0,
            "passed": (best_retrieval[1].get("recall_at_5", 0) >= 0.80) if best_retrieval else False,
            "best_mode": best_retrieval[0] if best_retrieval else None,
        },
    }


def run_phase_c3_evaluation(session, *, retrieval_limit: int = 50, router_use_c2: bool = True) -> PhaseC3Summary:
    summary = PhaseC3Summary()

    summary.retrieval_modes = benchmark_retrieval_modes(session, limit=retrieval_limit)
    summary.rerankers = benchmark_rerankers_local(session, limit=min(retrieval_limit, 50))

    router_summary = evaluate_router(use_c2=router_use_c2)
    summary.router = {
        "intent_accuracy": router_summary.intent_accuracy(),
        "agent_accuracy": router_summary.agent_accuracy(),
        "context_resolution_accuracy": router_summary.context_resolution_accuracy(),
        "clarification_accuracy": router_summary.clarification_correct / router_summary.total if router_summary.total else 0,
        "total": router_summary.total,
    }
    summary.per_category = evaluate_by_category(use_c2=router_use_c2)
    summary.acceptance = check_acceptance(summary)
    return summary


def write_phase_c3_results(summary: PhaseC3Summary) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase_c2_baseline": PHASE_C2_BASELINE,
        "retrieval_modes": summary.retrieval_modes,
        "rerankers": summary.rerankers,
        "router": summary.router,
        "per_category": summary.per_category,
        "acceptance_criteria": summary.acceptance,
    }
    path = OUT_DIR / "phase_c3_summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
