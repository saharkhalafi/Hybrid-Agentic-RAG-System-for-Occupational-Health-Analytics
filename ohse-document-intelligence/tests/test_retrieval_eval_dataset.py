"""Tests for production retrieval evaluation dataset."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from config.settings import PROJECT_ROOT
from retrieval.eval_builders import build_all_records
from retrieval.eval_gold_loader import load_gold_corpus
from retrieval.eval_metrics import mrr, ndcg_at_k, recall_at_k
from retrieval.eval_schema import RetrievalEvalRecord
from retrieval.eval_validator import validate_master, validate_records

MASTER = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master.jsonl"
STATS = PROJECT_ROOT / "data" / "retrieval_eval" / "dataset_statistics.json"


@pytest.fixture(scope="module")
def records() -> list[RetrievalEvalRecord]:
    if not MASTER.exists():
        pytest.skip("dataset not built — run scripts/build_retrieval_eval_dataset.py")
    return [RetrievalEvalRecord.model_validate(json.loads(line)) for line in MASTER.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_master_file_exists():
    assert MASTER.exists(), "run python scripts/build_retrieval_eval_dataset.py"


def test_dataset_size(records):
    assert len(records) >= 500


def test_numerical_subset_size(records):
    numeric = sum(1 for r in records if r.ground_truth.numeric_values)
    assert numeric >= 100


def test_formula_subset_size(records):
    formula = sum(1 for r in records if r.category == "formula" or "FORMULA" in r.intent)
    assert formula >= 50


def test_conversational_size(records):
    turns = sum(1 for r in records if r.session_id)
    assert turns >= 100
    assert len({r.session_id for r in records if r.session_id}) >= 30


def test_negative_adversarial_size(records):
    neg = sum(1 for r in records if r.category in {"negative", "adversarial"})
    assert neg >= 50


def test_persian_language(records):
    for r in records:
        assert r.language == "fa"
        assert r.answer_language == "fa"


def test_no_placeholder_queries(records):
    for r in records:
        assert "سؤال ارزیابی" not in r.query


def test_validation_no_critical_errors(records):
    issues = validate_records(records, corpus=load_gold_corpus())
    critical = [i for i in issues if i["level"] == "critical"]
    assert critical == [], critical[:5]


def test_intent_coverage(records):
    intents = Counter(r.intent for r in records)
    assert intents.get("STRUCTURED.OEL.TWA_LOOKUP", 0) >= 20
    assert intents.get("SEMANTIC.DEFINITION.TWA", 0) >= 1
    assert sum(1 for r in records if r.intent.startswith("HYBRID")) >= 5
    assert sum(1 for r in records if r.category == "hybrid") >= 5


def test_agent_coverage(records):
    agents = Counter(a for r in records for a in r.expected_agents)
    assert agents["structured"] >= 100
    assert agents["semantic"] >= 50
    assert agents["formula"] >= 20


def test_session_context_records(records):
    ctx = [r for r in records if r.requires_session_context]
    assert len(ctx) >= 10
    for r in ctx:
        assert r.session_id
        assert r.resolved_query


def test_numeric_grounding_has_provenance(records):
    for r in records:
        for num in r.ground_truth.numeric_values:
            assert num.source_row_key or num.source_table or num.source_cell_id


def test_semantic_ground_truth_chunk_ids(records):
    corpus = load_gold_corpus()
    chunk_ids = {c.chunk_id for c in corpus.semantic_chunks}
    for r in records:
        for rel in r.ground_truth.relevance:
            if rel.source_type == "semantic_text" and rel.relevance.value == "REQUIRED" and rel.chunk_id:
                assert rel.chunk_id in chunk_ids


def test_reproducibility():
    r1 = build_all_records(load_gold_corpus())
    r2 = build_all_records(load_gold_corpus())
    assert len(r1) == len(r2)
    assert {x.query_id for x in r1} == {x.query_id for x in r2}


def test_metrics_functions():
    retrieved = ["a", "b", "c"]
    relevant = {"b", "c"}
    assert recall_at_k(retrieved, relevant, 3) == 1.0
    assert mrr(retrieved, relevant) == 0.5
    assert ndcg_at_k(retrieved, {"b": 3, "c": 2}, 3) > 0


def test_validate_script_exits_zero():
    result = subprocess.run(
        [sys.executable, "scripts/validate_retrieval_eval_dataset.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_statistics_file(records):
    if STATS.exists():
        stats = json.loads(STATS.read_text(encoding="utf-8"))
        assert stats["total_records"] == len(records) or abs(stats["total_records"] - len(records)) < 100
