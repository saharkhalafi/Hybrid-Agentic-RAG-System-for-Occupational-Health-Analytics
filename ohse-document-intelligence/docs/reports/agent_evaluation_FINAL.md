# Phase C — Agent Evaluation

**Date:** 2026-08-09

## End-to-End Sample (100 queries, PostgreSQL live)

| Metric | Value |
|--------|-------|
| Intent accuracy | **97.00%** |
| Passed | 97 / 100 |
| Failed | 3 |

## Per-Category Router Intent Accuracy (full eval)

| Category | Intent Acc. | Agent Routing | N |
|----------|-------------|---------------|---|
| structured | **92.18%** | 93.77% | 1,957 |
| semantic | **86.65%** | 98.74% | 397 |
| hybrid | 50.00% | 50.00% | 80 |
| formula | 1.92% | 67.31% | 52 |
| conversational | 0.00% | 35.53% | 152 |
| adversarial | 95.24% | 95.24% | 42 |
| negative | 18.18% | 18.18% | 11 |
| clarification | 40.00% | 80.00% | 5 |

Output: `data/evaluation/agent_results.jsonl`

## Agent Behaviors Verified

### Structured Agent
- OEL lookups via `PostgresStructuredStore` → `oel_chemical_limits`
- Returns: value, unit, source_row_key, page_number, cell provenance
- Numeric values sourced from PostgreSQL only

### Semantic Agent
- Uses `search_persian_semantic()` with production filters
- Top-K=5 retrieval with scores and chunk citations

### Formula Agent
- Registry lookup from `formulas` table
- Deterministic `evaluate_formula_by_id()` for vibration ahv
- Missing inputs → CLARIFY; unsupported → guardrail

### Hybrid Agent
- Orchestrates structured + semantic + formula without duplicating logic
- Exposure comparison computed deterministically when concentration slot present

## Known Agent Limitations

1. Only `formula_240_01` (ahv) has deterministic calculation
2. Noise/vibration/biological structured tables empty — no structured agent support
3. Multi-word chemical alias resolution incomplete

## Reproduce

```bash
python scripts/run_phase_c_evaluation.py --mode e2e --limit 100
```

Results: `data/evaluation/e2e_results.jsonl`
