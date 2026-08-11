"""Retrieval evaluation metrics (Recall@K, MRR, NDCG, routing accuracy)."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Sequence


def _dedupe_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def relevance_score(level: str) -> int:
    return {"REQUIRED": 3, "SUPPORTING": 2, "OPTIONAL": 1, "IRRELEVANT": 0}.get(level, 0)


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return len(top & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = retrieved[:k]
    if not top:
        return 0.0
    return len(set(top) & relevant) / len(top)


def mrr(retrieved: Sequence[str], relevant: set[str]) -> float:
    for i, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], graded: dict[str, int], k: int) -> float:
    def dcg(scores: list[int]) -> float:
        return sum(s / math.log2(i + 2) for i, s in enumerate(scores))

    actual = [graded.get(r, 0) for r in retrieved[:k]]
    ideal = sorted(graded.values(), reverse=True)[:k]
    idcg = dcg(ideal)
    if idcg == 0:
        return 0.0
    return dcg(actual) / idcg


def hit_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    return 1.0 if set(retrieved[:k]) & relevant else 0.0


def routing_accuracy(expected: Sequence[str], predicted: Sequence[str]) -> float:
    if not expected:
        return 0.0
    exp = set(expected)
    pred = set(predicted)
    return len(exp & pred) / len(exp)


def intent_accuracy(expected: str, predicted: str) -> float:
    return 1.0 if expected == predicted else 0.0


def exact_numeric_match(expected: float | None, actual: float | None, tol: float = 1e-6) -> float:
    if expected is None or actual is None:
        return 0.0
    return 1.0 if abs(expected - actual) <= tol else 0.0


def aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}
    keys = rows[0].keys()
    out: dict[str, float] = {}
    for key in keys:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        out[key] = sum(vals) / len(vals) if vals else 0.0
    return out


def evaluate_retrieval_run(
    cases: list[dict[str, Any]],
    *,
    retrieved_key: str = "retrieved_ids",
    relevant_key: str = "relevant_ids",
) -> dict[str, float]:
    """Evaluate a batch of cases with retrieved and relevant id lists."""
    metrics_rows: list[dict[str, float]] = []
    for case in cases:
        retrieved = _dedupe_preserve(case.get(retrieved_key, []))
        relevant = set(case.get(relevant_key, []))
        graded = case.get("graded_relevance") or {rid: 3 for rid in relevant}
        metrics_rows.append({
            "recall@1": recall_at_k(retrieved, relevant, 1),
            "recall@3": recall_at_k(retrieved, relevant, 3),
            "recall@5": recall_at_k(retrieved, relevant, 5),
            "precision@1": precision_at_k(retrieved, relevant, 1),
            "precision@3": precision_at_k(retrieved, relevant, 3),
            "mrr": mrr(retrieved, relevant),
            "ndcg@5": ndcg_at_k(retrieved, graded, 5),
            "hit@1": hit_at_k(retrieved, relevant, 1),
            "hit@5": hit_at_k(retrieved, relevant, 5),
        })
    return aggregate_metrics(metrics_rows)
