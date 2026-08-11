#!/usr/bin/env python3
"""Build production retrieval evaluation dataset from Gold corpus."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from config.settings import PROJECT_ROOT
from retrieval.eval_builders import BUILDER_VERSION, build_all_records
from retrieval.eval_gold_loader import load_gold_corpus
from retrieval.eval_schema import DatasetStatistics, RetrievalEvalRecord

OUT_DIR = PROJECT_ROOT / "data" / "retrieval_eval"
CATEGORIES = (
    "structured",
    "semantic",
    "formula",
    "hybrid",
    "conversational",
    "clarification",
    "negative",
    "adversarial",
    "numerical",
    "citation",
    "end_to_end",
)


def _write_jsonl(path: Path, records: list[RetrievalEvalRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec.to_jsonl_dict(), ensure_ascii=False) + "\n")


def compute_statistics(records: list[RetrievalEvalRecord]) -> DatasetStatistics:
    stats = DatasetStatistics(total_records=len(records))
    stats.by_intent = dict(Counter(r.intent for r in records))
    stats.by_category = dict(Counter(r.category for r in records))
    agent_counter: Counter[str] = Counter()
    for r in records:
        for a in r.expected_agents:
            agent_counter[a] += 1
    stats.by_agent = dict(agent_counter)
    stats.numerical_queries = sum(1 for r in records if r.answer_type == "numeric" or r.ground_truth.numeric_values)
    stats.formula_queries = sum(1 for r in records if r.category == "formula" or "formula" in r.intent.lower())
    stats.conversational_turns = sum(1 for r in records if r.session_id)
    stats.conversational_sessions = len({r.session_id for r in records if r.session_id})
    stats.negative_adversarial = sum(1 for r in records if r.category in {"negative", "adversarial"})
    stats.unique_chemicals = len({r.slots.get("chemical_name") for r in records if r.slots.get("chemical_name")})
    stats.unique_chunks = len({
        rel.chunk_id
        for r in records
        for rel in r.ground_truth.relevance
        if rel.chunk_id
    })
    stats.unique_formulas = len({
        rel.formula_id
        for r in records
        for rel in r.ground_truth.relevance
        if rel.formula_id
    })
    stats.unique_pages = len({
        p
        for r in records
        for p in [
            r.metadata_requirements.page_number,
            *[rel.page_number for rel in r.ground_truth.relevance],
        ]
        if p
    })
    return stats


def main() -> None:
    corpus = load_gold_corpus()
    records = build_all_records(corpus)
    stats = compute_statistics(records)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Category subsets
    by_cat: dict[str, list[RetrievalEvalRecord]] = {c: [] for c in CATEGORIES}
    for rec in records:
        by_cat.setdefault(rec.category, []).append(rec)
        if rec.ground_truth.numeric_values:
            by_cat["numerical"].append(rec)
        if rec.citations:
            by_cat["citation"].append(rec)
        if len(rec.expected_agents) > 1 or rec.category == "hybrid":
            by_cat["end_to_end"].append(rec)

    master_path = OUT_DIR / "retrieval_eval_master.jsonl"
    _write_jsonl(master_path, records)

    for cat, cat_records in by_cat.items():
        if cat_records:
            _write_jsonl(OUT_DIR / cat / "eval.jsonl", cat_records)

    stats_path = OUT_DIR / "dataset_statistics.json"
    stats_path.write_text(stats.model_dump_json(indent=2), encoding="utf-8")

    print(json.dumps({
        "builder_version": BUILDER_VERSION,
        "master_path": str(master_path),
        "total_records": stats.total_records,
        "numerical": stats.numerical_queries,
        "formula": stats.formula_queries,
        "conversational_turns": stats.conversational_turns,
        "conversational_sessions": stats.conversational_sessions,
        "negative_adversarial": stats.negative_adversarial,
        "by_category": stats.by_category,
        "unique_chemicals": stats.unique_chemicals,
        "unique_chunks": stats.unique_chunks,
        "unique_formulas": stats.unique_formulas,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
