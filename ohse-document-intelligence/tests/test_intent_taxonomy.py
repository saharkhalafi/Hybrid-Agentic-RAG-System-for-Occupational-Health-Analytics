"""Validate production intent taxonomy datasets and routing safety."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from intent.taxonomy_schema import (
    TAXONOMY_DIR,
    load_taxonomy,
    validate_all,
    validate_eval_set,
    validate_examples,
    validate_routing_rules,
    validate_slots,
    validate_taxonomy,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


def test_taxonomy_file_exists():
    assert (TAXONOMY_DIR / "intent_taxonomy.yaml").exists()


def test_all_dataset_files_exist():
    for name in (
        "intent_taxonomy.yaml",
        "intent_examples_fa.jsonl",
        "intent_examples_en.jsonl",
        "intent_routing_rules.yaml",
        "intent_slots.yaml",
        "intent_negative_examples.jsonl",
        "intent_eval_set.jsonl",
    ):
        assert (TAXONOMY_DIR / name).exists(), f"missing {name}"


def test_taxonomy_validation_passes(taxonomy):
    issues = validate_taxonomy(taxonomy)
    assert issues == [], f"taxonomy issues: {issues}"


def test_examples_validation_passes(taxonomy):
    issues = validate_examples(taxonomy)
    assert issues == [], f"example issues: {issues}"


def test_eval_set_validation_passes(taxonomy):
    issues = validate_eval_set(taxonomy, min_queries=100)
    assert issues == [], f"eval issues: {issues}"


def test_routing_rules_validation_passes():
    issues = validate_routing_rules()
    assert issues == [], f"routing issues: {issues}"


def test_slots_validation_passes():
    issues = validate_slots()
    assert issues == [], f"slot issues: {issues}"


def test_validate_all_passes():
    issues = validate_all(min_eval=100)
    assert issues == [], f"validate_all issues: {issues}"


def test_intent_count_reasonable(taxonomy):
    assert 40 <= len(taxonomy.intents) <= 80


def test_persian_primary(taxonomy):
    assert taxonomy.language_primary == "fa"
    for intent in taxonomy.intents:
        assert intent.expected_answer_language == "fa"


def test_no_duplicate_intent_ids(taxonomy):
    ids = [i.intent_id for i in taxonomy.intents]
    assert len(ids) == len(set(ids))


def test_hybrid_intents_have_multiple_agents(taxonomy):
    hybrids = [i for i in taxonomy.intents if i.agent == "hybrid"]
    assert len(hybrids) >= 5
    for intent in hybrids:
        assert len(intent.hybrid_agents) >= 2


def test_clarify_intents_require_clarification(taxonomy):
    for intent in taxonomy.intents:
        if intent.domain == "CLARIFY":
            assert intent.requires_clarification
            assert intent.agent == "clarify"
            assert intent.missing_slots


def test_level2_numeric_not_semantic_only(taxonomy):
    for intent in taxonomy.intents:
        if intent.numeric_safety_level >= 2 and intent.agent == "semantic":
            assert intent.requires_clarification or not intent.supported


def test_schema_only_structured_marked_unsupported(taxonomy):
    schema_only = {
        "STRUCTURED.NOISE.LIMIT_LOOKUP",
        "STRUCTURED.VIBRATION.LIMIT_LOOKUP",
        "STRUCTURED.BIOLOGICAL.BEI_LOOKUP",
    }
    for intent in taxonomy.intents:
        if intent.intent_id in schema_only:
            assert intent.supported is False
            assert intent.fallback_agent == "semantic"


def test_fa_examples_count(taxonomy):
    fa_rows = [
        json.loads(line)
        for line in (TAXONOMY_DIR / "intent_examples_fa.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(fa_rows) >= 150
    counts = Counter(r["intent_id"] for r in fa_rows)
    supported = {i.intent_id for i in taxonomy.intents if i.supported}
    for iid in supported:
        assert counts.get(iid, 0) >= 1, f"{iid} has no fa JSONL examples"


def test_eval_no_placeholder_queries():
    eval_rows = [
        json.loads(line)
        for line in (TAXONOMY_DIR / "intent_eval_set.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in eval_rows:
        q = row["query"]
        assert "سؤال ارزیابی" not in q, q
        assert not q.startswith("سوال ارزیابی"), q


def test_eval_queries_are_domain_realistic(taxonomy):
    eval_rows = [
        json.loads(line)
        for line in (TAXONOMY_DIR / "intent_eval_set.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(eval_rows) >= 100
    eval_intents = {iid for r in eval_rows for iid in r["expected_intents"]}
    domains = {i.domain for i in taxonomy.intents if i.intent_id in eval_intents}
    assert "STRUCTURED" in domains
    assert "SEMANTIC" in domains
    assert "HYBRID" in domains or "CLARIFY" in domains


def test_routing_priority_forbids_semantic_oel():
    rules = yaml.safe_load((TAXONOMY_DIR / "intent_routing_rules.yaml").read_text(encoding="utf-8"))
    level2 = rules["numeric_authority_rules"]["level_2"]
    assert level2["authority"] == "postgresql"
    assert level2.get("forbid_semantic_numeric") is True


def test_agent_distribution(taxonomy):
    agents = Counter(i.agent for i in taxonomy.intents)
    assert agents["structured"] >= 5
    assert agents["semantic"] >= 10
    assert agents["formula"] >= 3
    assert agents["hybrid"] >= 5
    assert agents["clarify"] >= 3
    assert agents["guardrail"] >= 3
