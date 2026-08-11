# HSE6 Pipeline Architecture (v3)

Production contract for the OHSE document-intelligence platform. Each stage has a **typed artifact**, **immutable upstream references**, and **layered provenance**.

## Pipeline

```
PDF
 │
 ▼
Document AI Layout Parser
 │
 ▼
Immutable Evidence Store          ← never mutate after write
 │
 ▼
Canonical Grid Builder            ← parser-agnostic structure
 │
 ▼
Table Family Classifier           ← deterministic; Gemini fallback for ambiguity only
 │
 ▼
Schema Registry
 │
 ▼
Deterministic Semantic Mapper     ← Gemini only when headers are ambiguous
 │
 ▼
Validation Engine                 ← typed error codes
 │
 ▼
Domain PostgreSQL
 │
 ├────────► pgvector
 │
 └────────► RAG / benchmarks
```

## Stage contracts

| Stage | Module | Artifact path | Mutable? |
|-------|--------|---------------|----------|
| Evidence | `pipeline_contracts/evidence.py` | `data/evidence/{hash}_{pages}/` | **Never** |
| Structure | `pipeline_contracts/canonical_grid.py` | `data/canonical/grids/{hash}_{pages}/` | Derived |
| Classification | `pipeline_contracts/table_family_classifier.py` | embedded in Canonical Table | Derived |
| Semantics | `pipeline_contracts/canonical_table.py` | `data/canonical/tables/{hash}_{pages}/` | Derived |
| Domain | `persistence/evidence_pipeline.py` | PostgreSQL | Validated only |

### Evidence Layer (immutable)

Forensic source for every downstream stage:

```
data/evidence/{hash}_{start}-{end}/
  manifest.json
  document_ai_raw.json
  layout_entities.json
  pages/page_047.json
  tables/table_047_01.json
```

Rules:
- Write-once; skip overwrite unless `--force`
- Downstream stages reference `evidence_ref` paths, never copy raw OCR into gold

### Canonical Grid (parser-agnostic)

```json
{
  "table_id": "table_047_01",
  "page_number": 47,
  "source_processors": ["document_ai", "pymupdf_recovery"],
  "headers": [{ "cell_id": "...", "text": "TWA", "provenance": { ... } }],
  "rows": [{ "row_index": 1, "cells": [ ... ] }],
  "confidence": {
    "layout_confidence": 0.99,
    "geometry_confidence": 0.95
  },
  "evidence_refs": ["data/evidence/.../document_ai_raw.json"]
}
```

Consumers (classifier, mapper, validator) **must not** depend on Document AI vs PyMuPDF — only `CanonicalGrid`.

### Canonical Table (downstream contract)

The permanent semantic artifact. Validation, QA, RAG, benchmarks, and domain mapping consume **this**, not raw Document AI:

```json
{
  "table_id": "table_047_01",
  "schema": "chemical_oel_v1",
  "schema_version": "1",
  "headers": ["chemical_name", "CAS", "TWA", "STEL"],
  "rows": [{ "row_index": 0, "fields": { "CAS": { "value": "75-05-8", "cell_id": "..." } } }],
  "classification": { "schema_id": "chemical_oel_v1", "confidence": 0.98, "classifier": "deterministic_rules" },
  "confidence": {
    "layout_confidence": 0.99,
    "geometry_confidence": 0.95,
    "mapping_confidence": 0.98,
    "validation_confidence": 1.0
  },
  "validation": [{ "code": "MERGED_CELL_UNRESOLVED", "field": "chemical_name", "severity": "warning" }]
}
```

## Provenance (every artifact)

```json
{
  "processor": "document_ai",
  "processor_version": "0.1.0",
  "pipeline": "1.0.0",
  "schema": "chemical_oel_v1",
  "schema_version": "1",
  "confidence_source": "structural_resolver",
  "evidence_ref": "data/evidence/.../document_ai_raw.json"
}
```

## Layered confidence

Never collapse to a single opaque score in storage. Use:

| Layer | Source |
|-------|--------|
| `layout_confidence` | Document AI / OCR |
| `geometry_confidence` | Structural resolver / grid builder |
| `mapping_confidence` | Table family classifier + semantic mapper |
| `validation_confidence` | Validation engine (1.0 = PASS) |

Overall confidence = **minimum** of populated layers.

## Typed validation errors

| Code | Meaning |
|------|---------|
| `UNIT_MISMATCH` | Exposure value/unit inconsistent |
| `COLUMN_AMBIGUOUS` | Header maps to multiple schema fields |
| `MERGED_CELL_UNRESOLVED` | Merged cell not split — review required |
| `CAS_INVALID` | CAS format check failed |
| `HEADER_UNKNOWN` | No schema field for column |
| `ROW_ALIGNMENT_FAILED` | Row/column geometry inconsistent |
| `VALUE_NOT_IN_EVIDENCE` | Semantic value not traceable to cell |
| `CROSS_PAGE_REFERENCE` | Provenance spans pages illegally |
| `EXTRACTION_UNCERTAIN` | Parser flagged low confidence |
| `SCHEMA_UNKNOWN` | No registry entry for table family |
| `NUMERIC_NORMALIZATION` | Normalized fraction ≠ raw OCR |

## Gemini boundaries

| Task | Allowed? |
|------|----------|
| Table family classification (ambiguous headers) | Yes — schema_id only |
| Column role disambiguation (`حد مجاز` → TWA/STEL/C) | Yes — mapping only |
| Cell value extraction | **Never** |
| Table reconstruction from flat OCR | **Never** |
| CAS / ppm / MW inference | **Never** |

Deterministic aliases (e.g. `وزن مولکولی` → `molecular_weight`) live in schema registry — no LLM.

## Table extraction architectural rule

Table extraction is a **geometry-first, evidence-first** process. Physical table structure is reconstructed deterministically from Document AI and PDF geometry **before** any semantic interpretation. LLMs may map established columns to domain semantics, but may **never** create, remove, reorder, merge, split, or numerically reinterpret physical table data. Original values remain immutable and every normalized value must retain a direct provenance link to its source evidence.

### Required pipeline order

```
PDF → Raw evidence → Document AI / PyMuPDF geometry
  → Physical row/column reconstruction → Canonical Grid
  → Merged-cell resolution → Multi-row header reconstruction
  → Physical column IDs (C0…Cn) → Semantic header mapping
  → Row extraction → Numeric validation → Schema validation
  → Provenance validation → Gold / HITL
```

### Numeric integrity contract

```
raw_source_value → original_value → numeric_validation → normalized_value (if deterministic)
```

Never: `raw_source_value → LLM → interpreted number`

Each numeric cell carries: `original_value`, `normalized_value`, `numeric_parse_status`, `numeric_parse_method`, `source_cell_id`, `page_number`, `bbox`.

### Table quality gate

`TABLE_GOLD_ALLOWED` requires all of:

- `geometry_valid`
- `column_count_valid`
- `header_structure_valid`
- `merged_cells_valid`
- `numeric_integrity_valid`
- `provenance_complete`

Failure → `review_required` in PostgreSQL HITL; no silent gold promotion.

## Commands

```powershell
python scripts/generate_goldset.py --file "../OHE6.pdf" --start-page 44 --end-page 49 --force
python scripts/persist_evidence_pipeline.py --file "../OHE6.pdf" --start-page 44 --end-page 49
python scripts/build_rag_corpus.py
```

After generation, inspect:

- Evidence: `data/evidence/`
- Grids: `data/canonical/grids/`
- Tables: `data/canonical/tables/`
- Audit gold: `gold/tables/`, `gold/pages/`

## Scaling to 100+ table formats

Adding format #101:

1. Add schema JSON to `schema_registry/`
2. Add deterministic header aliases + optional parser module
3. Extend `TableFamilyClassifier` rules (Gemini fallback automatic)
4. **Do not** change evidence, grid, or validation contracts

That is the difference between a document platform and a collection of extraction scripts.
