# Phase B — Metadata / Provenance Contract

This document defines the shared metadata contract for the HSE6 knowledge layer.

## Traceability chain

```
answer
  → knowledge record (PostgreSQL)
  → Gold artifact (gold/tables, gold/formulas, gold/rag)
  → table / chunk / formula
  → cell / text
  → PDF page
  → immutable evidence (data/intermediate/evidence)
```

## Code contract

Implemented in `knowledge/metadata_contract.py`:

- `KnowledgeMetadata` — envelope for structured, semantic, and formula records
- `FieldProvenance` — separates `original_value`, `normalized_value`, `accepted_value`
- `SourceReference` — document, page, table, row, cell, chunk, formula, evidence path, bbox
- `ProvenanceChain` — audit trail for agent responses

## Validation gates

| Layer | Production gate |
|-------|-----------------|
| Structured tables | `gold/tables/*.json` with `gold_allowed=true` |
| Formulas | `gold/formulas/*.json` with `status=approved` |
| Semantic (Persian) | `review_status=accepted` only |
| Legacy QA (English) | `regulatory_qa.jsonl` stored as `legacy_reference` |

## Persian-first retrieval

- Semantic chunks preserve Persian `text` / `normalized_text`
- Structured entities support multilingual aliases (`fa`, `en`, `cas`)
- English legacy QA is not overwritten; stored separately for reference

## Versioning

- `gold_version` / `pipeline_version` on structured records
- `embedding_model`, `embedding_version`, `embedding_dimension`, `content_version` on semantic chunks
- `formula_version` on formula registry entries
