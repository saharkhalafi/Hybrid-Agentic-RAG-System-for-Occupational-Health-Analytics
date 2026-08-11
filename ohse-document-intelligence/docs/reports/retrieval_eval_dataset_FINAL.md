# Retrieval Evaluation Dataset — Final Report

**Project:** OHSE Document Intelligence (OHE6)  
**Date:** 2026-08-09  
**Status:** **READY WITH LIMITATIONS** (machine-validated; pending human review)

---

## Executive Summary

A production-grade, fully grounded retrieval evaluation dataset has been built from accepted Gold artifacts and PostgreSQL-synced sources. **No LLM-generated ground truth** — all numeric values, chunk IDs, row keys, and formula IDs are attached programmatically from Gold files.

| Metric | Value |
|--------|-------|
| Total records | **2,726** |
| Numerical queries | **1,998** |
| Formula queries | **55** |
| Conversational turns | **152** (61 sessions) |
| Negative + adversarial | **83** |
| Unique chemicals | **330** |
| Unique semantic chunks (grounded) | **372** |
| Unique formulas | **6** (all approved) |
| Critical validation errors | **0** |
| Test results | **18/18 passed** (retrieval eval) + **20/20** (intent taxonomy) |

---

## 1. Files Created

| File | Purpose |
|------|---------|
| `retrieval/eval_schema.py` | Pydantic schema (`RetrievalEvalRecord`, relevance levels, numeric grounding) |
| `retrieval/eval_gold_loader.py` | Load OEL rows, semantic chunks, formulas from Gold |
| `retrieval/eval_builders.py` | Deterministic query + ground-truth builders |
| `retrieval/eval_validator.py` | Provenance and schema validation |
| `retrieval/eval_metrics.py` | Recall@K, MRR, NDCG, routing metrics |
| `scripts/build_retrieval_eval_dataset.py` | Build master + category JSONL subsets |
| `scripts/validate_retrieval_eval_dataset.py` | Validation CLI (exit 1 on critical errors) |
| `scripts/evaluate_retrieval_dataset.py` | Metrics over retrieval run results |
| `tests/test_retrieval_eval_dataset.py` | Automated dataset tests |
| `data/retrieval_eval/retrieval_eval_master.jsonl` | Combined master dataset |
| `data/retrieval_eval/{category}/eval.jsonl` | Category subsets |
| `data/retrieval_eval/dataset_statistics.json` | Build statistics |
| `data/retrieval_eval/validation_report.json` | Latest validation output |

---

## 2. Dataset Categories

| Category | Records |
|----------|---------|
| structured | 1,957 |
| semantic | 397 |
| formula | 52 |
| hybrid | 80 |
| conversational | 152 |
| clarification | 5 |
| negative | 41 |
| adversarial | 42 |
| numerical (cross-cut) | 1,998 |
| citation (cross-cut) | (records with citations) |

Directory layout:

```text
data/retrieval_eval/
├── retrieval_eval_master.jsonl
├── dataset_statistics.json
├── validation_report.json
├── structured/eval.jsonl
├── semantic/eval.jsonl
├── formula/eval.jsonl
├── hybrid/eval.jsonl
├── conversational/eval.jsonl
├── clarification/eval.jsonl
├── negative/eval.jsonl
├── adversarial/eval.jsonl
├── numerical/eval.jsonl
├── citation/eval.jsonl
└── end_to_end/eval.jsonl
```

---

## 3. Ground Truth Design

### Structured (OEL)
- Source: `gold/tables/*.json` → `oel_chemical_limits` row keys
- Each numeric record includes: `source_row_key`, `cell_id`, `bbox`, `normalized_value`, `page_number`
- Authority: **PostgreSQL** (never semantic)

### Semantic
- Source: `gold/rag/semantic_text_production.jsonl` (563 accepted chunks)
- Relevance levels: `REQUIRED` (target chunk) + `SUPPORTING`
- 372 unique chunks referenced across 397 semantic eval records

### Formula
- Source: `gold/formulas/*.json` (6 approved formulas)
- Deterministic calculation grounded via `evaluate_vibration_daily_exposure()` for `formula_240_01`
- Unsupported formulas → `GUARDRAIL` / `FORMULA.CALCULATION.UNSUPPORTED`

### Conversational (session memory)
- `session_id`, `turn_id`, `previous_turn_ids`, `resolved_query`, `requires_session_context`
- 61 sessions including OEL follow-ups (e.g. «STEL نداره؟») and formula chains

---

## 4. Intent & Agent Coverage

Top intents by count:
- `STRUCTURED.OEL.TWA_LOOKUP`: 1,689
- `SEMANTIC.EXPLANATION.CONCEPT`: 221
- `STRUCTURED.OEL.STEL_LOOKUP`: 140
- `HYBRID.LOOKUP_AND_EXPLAIN`: 40
- `HYBRID.LOOKUP_COMPARE_EXPLAIN`: 40

Agent distribution (record-level):
- structured: dominant (OEL lookups)
- semantic: 397+ records
- formula: 55+ records
- hybrid: 80+ records
- clarify / guardrail: clarification + negative subsets

---

## 5. Review Status Workflow

All records start as:

```json
"review_status": "generated"
```

After `validate_retrieval_eval_dataset.py` passes:

→ eligible for `machine_validated`

Human review required before:

→ `approved` (evaluation Gold Set)

---

## 6. Validation Results

```
records: 2726
critical: 0
warnings: 109 (mostly duplicate_query near-duplicates, unnormalized_numeric for fraction strings)
```

Validation checks:
- ✅ chunk_id exists in Gold corpus
- ✅ source_row_key exists in Gold tables
- ✅ formula_id exists in Gold formulas
- ✅ intent in taxonomy (51 intents)
- ✅ Persian language required
- ✅ session context integrity
- ✅ no placeholder queries

---

## 7. Evaluation Metrics (implemented)

`retrieval/eval_metrics.py` supports:
- Recall@1/3/5/10, Precision@1/3/5
- MRR, NDCG@5/10, Hit@K
- Routing accuracy, intent accuracy
- Exact numeric match

Run after agents produce retrieval results:

```bash
python scripts/evaluate_retrieval_dataset.py --results path/to/run_results.jsonl
```

---

## 8. Known Limitations

1. **Noise/vibration/biological structured tables** — not populated; no structured eval for those domains (semantic-only content exists).
2. **Only 6 formulas** — formula eval capped at ~55 queries (variants + follow-ups); cannot reach hundreds without more Gold formulas.
3. **English chemical names in queries** — mirror Gold table `english_name` values (Persian queries use Persian templates where names exist).
4. **Not human-reviewed** — status remains `generated`; manual review required before production eval Gold.
5. **End-to-end answer eval** — dataset provides grounding; agents not implemented yet.
6. **109 warnings** — fraction-format numerics (`3/0`) and near-duplicate query templates; non-blocking.

---

## 9. Reproduce Commands

```bash
cd ohse-document-intelligence

# Build dataset from Gold (deterministic)
python scripts/build_retrieval_eval_dataset.py

# Validate (must exit 0)
python scripts/validate_retrieval_eval_dataset.py

# Tests
pytest tests/test_retrieval_eval_dataset.py -v
pytest tests/ -q
```

---

## 10. Recommended Human-Review Priorities

1. **Hybrid compare queries** — verify exposure comparison logic expectations
2. **Conversational resolved_query** — confirm Persian resolution wording
3. **Semantic REQUIRED vs SUPPORTING** — adjust relevance grades for NDCG
4. **Adversarial similar-name pairs** — confirm intended chemical disambiguation
5. **Sample numerical records** — spot-check `normalized_value` against PDF tables

---

## Verdict

**RETRIEVAL EVAL DATASET: READY WITH LIMITATIONS**

The dataset is machine-validated, fully grounded in Gold artifacts, suitable for measuring Structured/Semantic/Formula/Hybrid retrieval and routing — pending human review for `approved` status.
