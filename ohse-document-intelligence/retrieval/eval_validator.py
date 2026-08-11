"""Validation for retrieval evaluation dataset records."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from config.settings import PROJECT_ROOT
from retrieval.eval_gold_loader import load_gold_corpus
from retrieval.eval_schema import RetrievalEvalRecord

MASTER_PATH = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master.jsonl"
TAXONOMY_PATH = PROJECT_ROOT / "data" / "intent_taxonomy" / "intent_taxonomy.yaml"

CRITICAL = "critical"
WARNING = "warning"


def load_records(path: Path | None = None) -> list[RetrievalEvalRecord]:
    path = path or MASTER_PATH
    records: list[RetrievalEvalRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(RetrievalEvalRecord.model_validate(json.loads(line)))
    return records


def load_intent_ids() -> set[str]:
    raw = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    return {item["intent_id"] for item in raw.get("intents", [])}


def validate_records(
    records: list[RetrievalEvalRecord],
    *,
    corpus=None,
) -> list[dict[str, Any]]:
    corpus = corpus or load_gold_corpus()
    issues: list[dict[str, Any]] = []

    chunk_ids = {c.chunk_id for c in corpus.semantic_chunks}
    row_keys = {r.source_row_key for r in corpus.oel_rows}
    formula_ids = {f.formula_id for f in corpus.formulas}
    valid_intents = load_intent_ids()

    seen_queries: set[str] = set()
    seen_ids: set[str] = set()

    for rec in records:
        def add(level: str, code: str, msg: str) -> None:
            issues.append({"level": level, "code": code, "query_id": rec.query_id, "message": msg})

        if rec.query_id in seen_ids:
            add(CRITICAL, "duplicate_query_id", f"duplicate query_id {rec.query_id}")
        seen_ids.add(rec.query_id)

        if rec.query in seen_queries:
            add(WARNING, "duplicate_query", f"duplicate query text: {rec.query[:80]}")
        seen_queries.add(rec.query)

        if rec.intent not in valid_intents:
            add(CRITICAL, "unknown_intent", f"intent {rec.intent} not in taxonomy")

        if rec.language != "fa" or rec.answer_language != "fa":
            add(CRITICAL, "non_persian", "language must be fa")

        if not rec.expected_agents:
            add(CRITICAL, "missing_agents", "expected_agents empty")

        if rec.requires_clarification and "clarify" not in rec.expected_agents:
            add(CRITICAL, "clarify_routing", "requires_clarification but agent is not clarify")

        if rec.answer_type == "numeric" and not rec.ground_truth.numeric_values and rec.intent.startswith("STRUCTURED"):
            add(CRITICAL, "missing_numeric_gt", "numeric answer without numeric ground truth")

        for rel in rec.ground_truth.relevance:
            if rel.source_type == "semantic_text" and rel.chunk_id and rel.chunk_id not in chunk_ids:
                add(CRITICAL, "invalid_chunk_id", f"chunk_id {rel.chunk_id} not in gold corpus")
            if rel.source_type == "structured" and rel.record_id and rel.record_id not in row_keys:
                add(CRITICAL, "invalid_row_key", f"source_row_key {rel.record_id} not in gold tables")
            if rel.source_type == "formula" and rel.formula_id and rel.formula_id not in formula_ids:
                add(CRITICAL, "invalid_formula_id", f"formula_id {rel.formula_id} not in gold formulas")

        for num in rec.ground_truth.numeric_values:
            if num.normalized_value is None and rec.answer_type == "numeric":
                add(WARNING, "unnormalized_numeric", f"missing normalized_value for {rec.query_id}")
            if num.source_row_key and num.source_row_key not in row_keys:
                add(CRITICAL, "numeric_row_mismatch", f"numeric source_row_key {num.source_row_key} invalid")

        if rec.ground_truth.formula and rec.ground_truth.formula.formula_id not in formula_ids:
            add(CRITICAL, "formula_gt_invalid", f"formula {rec.ground_truth.formula.formula_id} not found")

        if rec.session_id and rec.turn_id > 1 and not rec.previous_turn_ids:
            add(CRITICAL, "missing_previous_turns", "session turn > 1 without previous_turn_ids")

        if rec.requires_session_context and not rec.session_id:
            add(CRITICAL, "session_context_missing", "requires_session_context but no session_id")

        if "سؤال ارزیابی" in rec.query:
            add(CRITICAL, "placeholder_query", "placeholder query text")

        if rec.expected_answer and re.search(r"^[A-Za-z0-9\s.,\-]+$", rec.expected_answer) and rec.answer_language == "fa":
            if rec.category not in {"formula"} and not rec.legacy_reference_en:
                # numeric answers like "0.3 ppm" are valid
                if not re.search(r"\d", rec.expected_answer):
                    add(WARNING, "english_answer", "expected_answer appears English-only")

    return issues


def validate_master(path: Path | None = None) -> tuple[list[RetrievalEvalRecord], list[dict[str, Any]]]:
    records = load_records(path)
    issues = validate_records(records)
    return records, issues
