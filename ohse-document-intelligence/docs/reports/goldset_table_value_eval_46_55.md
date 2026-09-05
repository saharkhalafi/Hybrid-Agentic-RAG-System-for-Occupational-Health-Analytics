# Goldset table-value evaluation (pages 46–55)

Generated from the existing evaluator. Scoring was not changed.

## Evaluator

- Comparison module: `tests/test_goldset_excel_46_55.py`
  - `load_excel_gold()`, `load_actual()`, `extract_actual_rows()`, `find_actual_row()`, `compare_field()`
- Official STEL/TWA scorer: `scripts/run_production_ingestion.py` → `validate_stel_twa()`
- Gold: `goldset.xlsx` (`page | row | CAS | chemical_name | STEL | TWA | MW`)
- Actual / “retrieved” values: `data/intermediate/validated_structure_46-55.json` Layer-2 cells
  - column 2 → STEL, column 3 → TWA, column 4 → MW, column 5 → chemical_name
- Match key: **page + CAS intersection** (`find_actual_row`). Physical Gold `row` is not the identity.
- Unmatched Gold rows are **excluded** from official STEL/TWA denominators (same as `validate_stel_twa`).

This evaluator is **table-value extraction** against Layer-2 structure. It does **not** call `QueryOrchestrator`, `PostgresStructuredStore.lookup_oel_field`, or `GoldsetQAEvaluator`. Query-time retrieval is a different artifact (`Goldset_QA.xlsx` / `scripts/evaluate_goldset_qa.py`).

Machine-readable per-row dump: `data/evaluation/goldset_table_value_46_55.json`

## Official metrics (`validate_stel_twa`)

| Metric | Value |
|--------|-------|
| Gold rows | 81 |
| Scored (matched page+CAS) | 79 |
| Unscored (no Actual match) | 2 |
| STEL correct | 71 / 79 (**89.87%**) |
| TWA correct | 72 / 79 (**91.14%**) |
| Rows with both STEL and TWA correct | 68 / 79 (86.08%) |
| Rows with STEL and/or TWA incorrect | 11 |

`chemical_name` never matches Gold under `compare_field` (79/79 matched rows): Layer-2 column 5 is the full OCR name cell (Persian + English + brackets), Gold is a short English label. That is **not** part of official STEL/TWA scoring. MW mismatches: 17/79.

## Failure categories (incorrect STEL/TWA or unscored)

These labels are post-hoc on existing mismatches. They do not change scoring.

| Category | Count | Meaning in this run |
|----------|------:|---------------------|
| EXTRACTION | 8 | Layer-2 STEL/TWA cell empty or different number vs Gold |
| GOLDSET | 4 | Gold identity or Gold limit token cannot be compared as a numeric limit |
| GEOMETRY | 1 | Layer-2 STEL/TWA are a column swap of Gold |
| CANONICAL_DATA | 0 | Evaluator does not read `oel_chemical_limits` |
| RETRIEVAL | 0 | Evaluator does not run semantic/structured query retrieval |
| QUERY/RESOLUTION | 0 | Evaluator does not run `chemical_resolver` / classifier |
| ROW_RECONSTRUCTION | 0 | Unmatched rows here are Gold identity, not a merged Layer-2 row |
| EVALUATOR | 0 | `compare_field` / `find_actual_row` behaved as designed |

## Representative failures

### GOLDSET — unmatched (unscored)

| page | row | Gold CAS | chemical_name | Cause |
|------|-----|----------|---------------|--------|
| 48 | 17 | `—` | acrylic acid polymer | No CAS; `find_actual_row` requires CAS |
| 51 | 44 | Excel `datetime(7789, 9, 5)` | ammonium dichromate, as chromium | CAS `7789-09-5` stored as a date in Excel |

### GOLDSET — scored but non-numeric Gold limits

| page | row | CAS | Gold STEL/TWA | Layer-2 | Cause |
|------|-----|-----|---------------|---------|--------|
| 47 | 12 | 74-86-2 | asphyxiant Persian text in both | empty | Gold put a narrative note in limit columns |
| 55 | 78 | 205-99-2 | TWA `alara` | empty | Gold token is not a numeric limit |

### GEOMETRY — STEL/TWA swap

| page | row | CAS | Gold | Layer-2 |
|------|-----|-----|------|---------|
| 49 | 26 | 107-11-9 | STEL 2 ppm / TWA 6 ppm | STEL 6 ppm / TWA 2 ppm |

This is the failure mode `TableGoldGenerator._overlay_pdf_stel_twa` + `recover_stel_twa_from_words` were built to repair **after** Layer-2. This evaluator scores **pre-overlay** columns 2/3.

### EXTRACTION — empty or wrong Layer-2 limits (examples)

| page | row | CAS | Gold | Layer-2 | Likely layer |
|------|-----|-----|------|---------|--------------|
| 46 | 5 | 64-19-7 | STEL `15ppm C 40ppm`, TWA 10 ppm | STEL empty, TWA 10 ppm | Cell/merged STEL+C not in column 2 |
| 46 | 6 | 108-24-7 | STEL 3 / TWA 1 | STEL empty | Missing STEL cell text |
| 47 | 9 | 75-05-8 | STEL 40 / TWA 20 | STEL empty | Missing STEL cell text |
| 47 | 13 | 79-27-6 | TWA `ppm1.0` | TWA `0.1ppm` | Numeric disagreement (Gold 1.0 vs Actual 0.1) |
| 53 | 61 | 1332-21-4 | TWA `0.1f/cc` | TWA empty, MW `61` | Fiber unit / column bleed |
| 54 | 65 | 492-80-8 | STEL 0.32 / TWA 0.08 mg/m³ | both empty | Limits not in Layer-2 cells |
| 54 | 67 | 123-77-3 | STEL 3 / TWA 1 mg/m³ | STEL empty | Missing STEL |
| 55 | 79 | 50-32-8 | STEL 0.02 / TWA 0.005 mg/m³ | both empty | Limits not in Layer-2 cells |

**Wrong extraction vs wrong retrieval:** every scored mismatch above is Layer-2 cell content vs Gold. None of these rows were failed by query routing or `oel_chemical_limits` lookup, because this evaluator never reaches those layers.
