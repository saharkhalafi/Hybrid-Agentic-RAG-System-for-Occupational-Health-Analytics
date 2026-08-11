#!/usr/bin/env python3
"""Build Phase C.2 enriched evaluation datasets (formula + conversational)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from config.settings import PROJECT_ROOT
from retrieval.eval_builders import BUILDER_VERSION, build_all_records, dedupe_records
from retrieval.eval_conversational_builder import build_conversational_eval_dataset
from retrieval.eval_formula_builder import build_formula_eval_dataset
from retrieval.eval_gold_loader import load_gold_corpus
from retrieval.eval_schema import DatasetStatistics, RetrievalEvalRecord

OUT_DIR = PROJECT_ROOT / "data" / "retrieval_eval"
PHASE_C2_MASTER = OUT_DIR / "retrieval_eval_master_c2.jsonl"


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
    stats.formula_queries = sum(1 for r in records if r.category == "formula")
    stats.conversational_turns = sum(1 for r in records if r.session_id)
    stats.conversational_sessions = len({r.session_id for r in records if r.session_id})
    return stats


def main() -> None:
    corpus = load_gold_corpus()

    # Base records (structured, semantic, hybrid, etc.) — keep existing
    base_records = build_all_records(corpus)

    # Phase C.2 enriched datasets
    formula_records = build_formula_eval_dataset(corpus)
    conv_records = build_conversational_eval_dataset(corpus)

    # Replace old formula/conversational with enriched versions
    non_formula_conv = [
        r for r in base_records if r.category not in {"formula", "conversational"}
    ]
    all_records = dedupe_records(non_formula_conv + formula_records + conv_records)

    _write_jsonl(PHASE_C2_MASTER, all_records)
    _write_jsonl(OUT_DIR / "formula" / "eval_c2.jsonl", formula_records)
    _write_jsonl(OUT_DIR / "conversational" / "eval_c2.jsonl", conv_records)

    # Also update category eval files
    _write_jsonl(OUT_DIR / "formula" / "eval.jsonl", formula_records)
    _write_jsonl(OUT_DIR / "conversational" / "eval.jsonl", conv_records)

    stats = compute_statistics(all_records)
    stats_path = OUT_DIR / "dataset_statistics_c2.json"
    stats_path.write_text(json.dumps(stats.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "master": str(PHASE_C2_MASTER),
        "total_records": len(all_records),
        "formula_records": len(formula_records),
        "conversational_turns": len(conv_records),
        "conversational_sessions": len({r.session_id for r in conv_records if r.session_id}),
        "builder_version": BUILDER_VERSION,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
