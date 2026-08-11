# Evidence Pipeline Architecture (v2)

Production architecture for HSE6 table extraction with PostgreSQL as the deterministic truth layer.

## Your design vs what exists

Your proposed architecture is **correct**. The codebase already had ~70% of the schema; the gap was **two disconnected pipelines** (JSON goldset vs DB `process_document.py`).

| Your layer | Status | Implementation |
|------------|--------|----------------|
| Immutable Evidence Store | **Added** | `document_evidence_snapshots` + `data/intermediate/document_ai_raw_*.json` (never mutated) |
| Universal Table Layer | **Exists + extended** | `extracted_tables`, `table_cells` + `evidence_cell_id`, `merged_cell`, `source` |
| Geometry Resolver | **Exists** | `document_ai/geometry_resolver.py` + `goldset_generator/structural_resolver.py` |
| Schema Registry | **Added** | `schema_registry/chemical_oel_v1.json` |
| Table Understanding (Gemini) | **Deferred** | Deterministic mapping from `table_gold_generator` for now; Gemini only for role hints later |
| Validation Engine | **Added** | `validation_engine/` (CAS, exposure, merged-cell rules) |
| Domain DB | **Wired** | `validated_table_rows` + `oel_chemical_limits` with `source_cell_provenance` |
| pgvector RAG | **Schema only** | `document_chunks.embedding` — embeddings in next phase |
| Human Review | **Exists** | `review_queue` + `requires_review` on validated rows |

## Recommended pipeline (pages 44–49)

```
PDF
  → Document AI Layout Parser (cached, immutable snapshot)
  → Universal Cell Store (extracted_tables + table_cells + evidence_cell_id)
  → Geometry Resolver + PyMuPDF recovery (structural_resolver)
  → Table Gold (deterministic column mapping)
  → Schema Registry (chemical_oel_v1)
  → Validation Engine
  → validated_table_rows + oel_chemical_limits (PostgreSQL)
  → Gold JSON (pages/tables — audit + benchmarks)
  → [Phase 2] pgvector indexes + Label Studio review
```

## Key design decisions

### 1. Gemini is NOT an extractor

Gemini may later assign **semantic_role** to columns (mapping only). Values always come from cells via `fact_resolver.py`.

### 2. Stable IDs across layers

```
evidence_cell_id: cell_table_047_01_1_5   (immutable, traceable)
       ↓
table_cells.evidence_cell_id
       ↓
validated_table_rows.field_provenance.CAS.cell_id
       ↓
oel_chemical_limits.source_cell_provenance
```

### 3. Domain rows only after validation

Never write `oel_chemical_limits` directly from Document AI. Only from `table_gold` + validation pass.

### 4. Merged cells → review, not split

`merged_cell=true` on `table_cells`; `requires_review=true` on `validated_table_rows`.

## Commands

```powershell
# 1. Start Postgres
docker compose up -d

# 2. Migrate
alembic upgrade head

# 3. Generate gold (file artifacts)
python scripts/generate_goldset.py --file "../OHE6.pdf" --start-page 44 --end-page 49 --force

# 4. Persist evidence → PostgreSQL
python scripts/persist_evidence_pipeline.py --file "../OHE6.pdf" --start-page 44 --end-page 49
```

## Phase 2 (next)

1. **Gemini Table Understanding Agent** — column role mapping only, input = validated grid + schema registry
2. **Three pgvector indexes** — evidence, regulatory narrative, row knowledge
3. **Label Studio** — sync `requires_review` rows
4. **Unify** `process_document.py` with `structural_resolver` (retire duplicate path)

## Why this is better than PDF → Gemini → DB

Page 47 failed because Gemini saw raw OCR before row/column structure existed, and could reference cells from other pages. The fix is architectural: **structure first, semantics second, domain last**.
