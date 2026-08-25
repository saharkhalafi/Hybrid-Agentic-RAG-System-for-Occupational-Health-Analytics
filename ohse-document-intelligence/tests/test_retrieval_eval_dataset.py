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
from retrieval.eval_validator import validate_records


MASTER = (
    PROJECT_ROOT
    / "data"
    / "retrieval_eval"
    / "retrieval_eval_master.jsonl"
)

STATS = (
    PROJECT_ROOT
    / "data"
    / "retrieval_eval"
    / "dataset_statistics.json"
)


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture(scope="module")
def corpus():
    return load_gold_corpus()


@pytest.fixture(scope="module")
def records() -> list[RetrievalEvalRecord]:
    if not MASTER.exists():
        pytest.skip(
            "dataset not built — run "
            "python scripts/build_retrieval_eval_dataset.py"
        )

    return [
        RetrievalEvalRecord.model_validate(json.loads(line))
        for line in MASTER.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


# ============================================================================
# BASIC DATASET CONTRACT
# ============================================================================


def test_master_file_exists():
    assert MASTER.exists(), (
        "Retrieval evaluation dataset is missing. "
        "Run: python scripts/build_retrieval_eval_dataset.py"
    )


def test_dataset_size(records):
    assert len(records) >= 500


def test_numerical_subset_size(records):
    numeric = sum(
        1
        for r in records
        if r.ground_truth.numeric_values
    )

    assert numeric >= 100


def test_formula_subset_size(records):
    formula = sum(
        1
        for r in records
        if (
            r.category == "formula"
            or "FORMULA" in r.intent
        )
    )

    assert formula >= 50


def test_conversational_size(records):
    turns = sum(
        1
        for r in records
        if r.session_id
    )

    sessions = {
        r.session_id
        for r in records
        if r.session_id
    }

    assert turns >= 100
    assert len(sessions) >= 30


def test_negative_adversarial_size(records):
    neg = sum(
        1
        for r in records
        if r.category in {
            "negative",
            "adversarial",
        }
    )

    assert neg >= 50


# ============================================================================
# LANGUAGE / QUERY QUALITY
# ============================================================================


def test_persian_language(records):
    for r in records:
        assert r.language == "fa"
        assert r.answer_language == "fa"


def test_no_placeholder_queries(records):
    for r in records:
        assert "سؤال ارزیابی" not in r.query


# ============================================================================
# GOLD / SOURCE VALIDATION
# ============================================================================


def test_validation_no_critical_errors(
    records,
    corpus,
):
    """
    Every retrieval-eval record must reference the current Gold corpus.

    This test intentionally does NOT ignore invalid row keys.

    If the Gold corpus changed after the dataset was generated, the correct
    fix is to rebuild retrieval_eval_master.jsonl rather than weakening this
    assertion.
    """

    issues = validate_records(
        records,
        corpus=corpus,
    )

    critical = [
        issue
        for issue in issues
        if issue["level"] == "critical"
    ]

    if critical:
        preview = "\n".join(
            (
                f"- {issue.get('query_id')}: "
                f"{issue.get('code')}: "
                f"{issue.get('message')}"
            )
            for issue in critical[:20]
        )

        pytest.fail(
            "Retrieval evaluation dataset is not synchronized "
            "with the current Gold corpus.\n\n"
            f"Critical issues: {len(critical)}\n"
            f"First issues:\n{preview}\n\n"
            "Rebuild the dataset with:\n"
            "python scripts/build_retrieval_eval_dataset.py"
        )


def test_numeric_grounding_has_provenance(records):
    """
    Every numeric ground-truth value must have traceable provenance.
    """

    failures = []

    for record in records:
        for numeric in record.ground_truth.numeric_values:

            if not (
                numeric.source_row_key
                or numeric.source_table
                or numeric.source_cell_id
            ):
                failures.append(
                    (
                        record.query_id,
                        numeric,
                    )
                )

    assert not failures, (
        "Numeric ground truth contains values without provenance. "
        f"First failures: {failures[:10]}"
    )


def test_semantic_ground_truth_chunk_ids(
    records,
    corpus,
):
    chunk_ids = {
        chunk.chunk_id
        for chunk in corpus.semantic_chunks
    }

    failures = []

    for record in records:
        for relevance in record.ground_truth.relevance:

            if (
                relevance.source_type
                == "semantic_text"
                and relevance.relevance.value
                == "REQUIRED"
                and relevance.chunk_id
            ):
                if relevance.chunk_id not in chunk_ids:
                    failures.append(
                        (
                            record.query_id,
                            relevance.chunk_id,
                        )
                    )

    assert not failures, (
        "Retrieval dataset references semantic chunks "
        "that do not exist in the current Gold corpus.\n"
        f"First failures: {failures[:10]}"
    )


# ============================================================================
# COVERAGE
# ============================================================================


def test_intent_coverage(records):
    intents = Counter(
        r.intent
        for r in records
    )

    assert (
        intents.get(
            "STRUCTURED.OEL.TWA_LOOKUP",
            0,
        )
        >= 20
    )

    assert (
        intents.get(
            "SEMANTIC.DEFINITION.TWA",
            0,
        )
        >= 1
    )

    assert sum(
        1
        for r in records
        if r.intent.startswith("HYBRID")
    ) >= 5

    assert sum(
        1
        for r in records
        if r.category == "hybrid"
    ) >= 5


def test_agent_coverage(records):
    agents = Counter(
        agent
        for record in records
        for agent in record.expected_agents
    )

    assert agents["structured"] >= 100
    assert agents["semantic"] >= 50
    assert agents["formula"] >= 20


def test_session_context_records(records):
    context_records = [
        r
        for r in records
        if r.requires_session_context
    ]

    assert len(context_records) >= 10

    for record in context_records:
        assert record.session_id
        assert record.resolved_query


# ============================================================================
# REPRODUCIBILITY
# ============================================================================


def test_reproducibility(corpus):
    first = build_all_records(corpus)
    second = build_all_records(corpus)

    assert len(first) == len(second)

    assert {
        record.query_id
        for record in first
    } == {
        record.query_id
        for record in second
    }


# ============================================================================
# METRICS
# ============================================================================


def test_metrics_functions():
    retrieved = [
        "a",
        "b",
        "c",
    ]

    relevant = {
        "b",
        "c",
    }

    assert recall_at_k(
        retrieved,
        relevant,
        3,
    ) == 1.0

    assert mrr(
        retrieved,
        relevant,
    ) == 0.5

    assert ndcg_at_k(
        retrieved,
        {
            "b": 3,
            "c": 2,
        },
        3,
    ) > 0


# ============================================================================
# VALIDATION SCRIPT
# ============================================================================


def test_validate_script_exits_zero():
    """
    The production validation script must pass against the current dataset.

    We intentionally keep this strict. If the Gold corpus changes, the
    retrieval evaluation dataset must be regenerated.
    """

    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_retrieval_eval_dataset.py",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    output = (
        result.stdout
        + result.stderr
    )

    assert result.returncode == 0, (
        "Retrieval evaluation validation failed.\n\n"
        f"{output}"
    )


# ============================================================================
# STATISTICS
# ============================================================================


def test_statistics_file(records):
    """
    Statistics are an artifact of the exact master dataset and therefore
    should match it exactly.
    """

    if not STATS.exists():
        pytest.fail(
            "dataset_statistics.json is missing. "
            "Rebuild the retrieval evaluation dataset."
        )

    stats = json.loads(
        STATS.read_text(
            encoding="utf-8"
        )
    )

    assert stats["total_records"] == len(
        records
    ), (
        "dataset_statistics.json is stale: "
        f"statistics={stats['total_records']} "
        f"actual={len(records)}"
    )