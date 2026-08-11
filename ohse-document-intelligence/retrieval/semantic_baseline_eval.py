"""Semantic retrieval baseline evaluation (Phase C.2)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT
from persistence.semantic_store import search_persian_semantic
from retrieval.eval_metrics import (
    evaluate_retrieval_run,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from retrieval.reranker import (
    LexicalReranker,
    NoOpReranker,
    RankedCandidate,
    Reranker,
    candidates_from_search_results,
    get_available_rerankers,
)

MASTER = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master.jsonl"
SEMANTIC_EVAL = PROJECT_ROOT / "data" / "retrieval_eval" / "semantic" / "eval.jsonl"
OUT_DIR = PROJECT_ROOT / "data" / "evaluation"


@dataclass
class SemanticEvalCase:
    query_id: str
    query: str
    relevant_chunk_ids: set[str]
    graded: dict[str, int]
    page_number: int | None = None


@dataclass
class SemanticEvalResult:
    reranker_name: str
    candidate_k: int
    final_k: int = 5
    recall_at_1: float = 0.0
    recall_at_3: float = 0.0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    recall_at_20: float = 0.0
    mrr: float = 0.0
    ndcg_at_5: float = 0.0
    precision_at_5: float = 0.0
    avg_latency_ms: float = 0.0
    total_queries: int = 0
    metadata_mode: str = "vector_only"
    failures: list[dict[str, Any]] = field(default_factory=list)


def load_semantic_eval_cases(limit: int | None = None) -> list[SemanticEvalCase]:
    path = SEMANTIC_EVAL if SEMANTIC_EVAL.exists() else MASTER
    cases: list[SemanticEvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if path == MASTER and rec.get("category") != "semantic":
            continue
        rel_ids: set[str] = set()
        graded: dict[str, int] = {}
        page = rec.get("metadata_requirements", {}).get("page_number")
        if isinstance(rec.get("metadata_requirements"), dict):
            page = rec["metadata_requirements"].get("page_number")
        for item in rec.get("ground_truth", {}).get("relevance", []):
            cid = item.get("chunk_id")
            if cid:
                rel_ids.add(cid)
                level = item.get("relevance", "REQUIRED")
                graded[cid] = {"REQUIRED": 3, "SUPPORTING": 2, "OPTIONAL": 1}.get(level, 3)
        if not rel_ids:
            chunk_id = None
            if isinstance(rec.get("metadata_requirements"), dict):
                chunk_id = rec["metadata_requirements"].get("chunk_id")
            if chunk_id:
                rel_ids.add(chunk_id)
                graded[chunk_id] = 3
        if not rel_ids:
            continue
        cases.append(
            SemanticEvalCase(
                query_id=rec.get("query_id", ""),
                query=rec["query"],
                relevant_chunk_ids=rel_ids,
                graded=graded,
                page_number=page,
            )
        )
        if limit and len(cases) >= limit:
            break
    return cases


def _metadata_filter(candidates: list[RankedCandidate], page: int | None) -> list[RankedCandidate]:
    if page is None:
        return candidates
    filtered = [c for c in candidates if c.metadata.get("page_number") == page or c.metadata.get("page") == page]
    return filtered if filtered else candidates


def _metadata_boost(candidates: list[RankedCandidate], page: int | None) -> list[RankedCandidate]:
    if page is None:
        return candidates
    boosted: list[RankedCandidate] = []
    for c in candidates:
        cp = c.metadata.get("page_number") or c.metadata.get("page")
        bonus = 0.05 if cp == page else 0.0
        c.score = min(1.0, c.score + bonus)
        boosted.append(c)
    boosted.sort(key=lambda x: x.score, reverse=True)
    return boosted


def evaluate_semantic_retrieval(
    session,
    *,
    reranker: Reranker | None = None,
    candidate_k: int = 20,
    final_k: int = 5,
    metadata_mode: str = "vector_only",
    limit: int | None = None,
) -> SemanticEvalResult:
    """Run semantic retrieval evaluation with optional reranker."""
    reranker = reranker or NoOpReranker()
    cases = load_semantic_eval_cases(limit)
    latencies: list[float] = []
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for case in cases:
        t0 = time.perf_counter()
        try:
            raw = search_persian_semantic(session, case.query, limit=candidate_k)
        except Exception as exc:
            failures.append({"query_id": case.query_id, "error": str(exc)})
            continue

        candidates = candidates_from_search_results(raw)

        if metadata_mode == "metadata_filter":
            candidates = _metadata_filter(candidates, case.page_number)
        elif metadata_mode == "metadata_boost":
            candidates = _metadata_boost(candidates, case.page_number)

        ranked = reranker.rerank(case.query, candidates, top_k=final_k)
        retrieved_ids = [c.chunk_id for c in ranked]
        latencies.append((time.perf_counter() - t0) * 1000)

        rows.append({
            "retrieved_ids": retrieved_ids,
            "relevant_ids": list(case.relevant_chunk_ids),
            "graded_relevance": case.graded,
        })

        if not recall_at_k(retrieved_ids, case.relevant_chunk_ids, 5):
            if len(failures) < 20:
                failures.append({
                    "query_id": case.query_id,
                    "query": case.query,
                    "expected": list(case.relevant_chunk_ids),
                    "got": retrieved_ids[:5],
                })

    agg = evaluate_retrieval_run(rows) if rows else {}
    n = len(rows)
    recall_rows = rows

    def _mean_recall(k: int) -> float:
        if not recall_rows:
            return 0.0
        return sum(recall_at_k(r["retrieved_ids"], set(r["relevant_ids"]), k) for r in recall_rows) / len(recall_rows)

    return SemanticEvalResult(
        reranker_name=reranker.name,
        candidate_k=candidate_k,
        final_k=final_k,
        recall_at_1=_mean_recall(1),
        recall_at_3=_mean_recall(3),
        recall_at_5=_mean_recall(5),
        recall_at_10=_mean_recall(10) if candidate_k >= 10 else _mean_recall(min(10, candidate_k)),
        recall_at_20=_mean_recall(20) if candidate_k >= 20 else _mean_recall(candidate_k),
        mrr=agg.get("mrr", 0.0),
        ndcg_at_5=agg.get("ndcg@5", 0.0),
        precision_at_5=agg.get("precision@5", 0.0) if "precision@5" in agg else (
            sum(precision_at_k(r["retrieved_ids"], set(r["relevant_ids"]), 5) for r in recall_rows) / n if n else 0.0
        ),
        avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0.0,
        total_queries=n,
        metadata_mode=metadata_mode,
        failures=failures,
    )


def benchmark_rerankers(
    session,
    *,
    candidate_ks: list[int] | None = None,
    limit: int | None = None,
) -> list[SemanticEvalResult]:
    """Benchmark all available rerankers across candidate pool sizes."""
    candidate_ks = candidate_ks or [10, 20, 30]
    results: list[SemanticEvalResult] = []

    # Baseline vector-only at K=1,3,5,10,20
    for k in [1, 3, 5, 10, 20]:
        r = evaluate_semantic_retrieval(
            session,
            reranker=NoOpReranker(),
            candidate_k=k,
            final_k=k,
            metadata_mode="vector_only",
            limit=limit,
        )
        r.reranker_name = f"baseline_top_{k}"
        results.append(r)

    for ck in candidate_ks:
        for reranker in get_available_rerankers():
            if reranker.name == "baseline_no_rerank":
                r = evaluate_semantic_retrieval(
                    session, reranker=reranker, candidate_k=ck, final_k=5, limit=limit
                )
            else:
                r = evaluate_semantic_retrieval(
                    session, reranker=reranker, candidate_k=ck, final_k=5, limit=limit
                )
            results.append(r)

    # Metadata experiments at candidate_k=20
    for mode in ["vector_only", "metadata_filter", "metadata_boost"]:
        r = evaluate_semantic_retrieval(
            session,
            reranker=LexicalReranker() if mode != "vector_only" else NoOpReranker(),
            candidate_k=20,
            final_k=5,
            metadata_mode=mode,
            limit=limit,
        )
        r.reranker_name = f"{mode}"
        results.append(r)

    return results


def write_reranker_results(results: list[SemanticEvalResult]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "reranker_results.jsonl"
    rows = []
    for r in results:
        rows.append({
            "reranker": r.reranker_name,
            "candidate_k": r.candidate_k,
            "final_k": r.final_k,
            "metadata_mode": r.metadata_mode,
            "recall_at_1": r.recall_at_1,
            "recall_at_3": r.recall_at_3,
            "recall_at_5": r.recall_at_5,
            "recall_at_10": r.recall_at_10,
            "recall_at_20": r.recall_at_20,
            "mrr": r.mrr,
            "ndcg_at_5": r.ndcg_at_5,
            "precision_at_5": r.precision_at_5,
            "avg_latency_ms": r.avg_latency_ms,
            "total_queries": r.total_queries,
            "failures_sample": r.failures[:5],
        })
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
