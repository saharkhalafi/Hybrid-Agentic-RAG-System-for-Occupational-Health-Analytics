# Phase B — Knowledge Engineering FINAL Report

**Date:** 2026-08-09  
**Project:** OHSE Document Intelligence (HSE6)  
**Scope:** Phase B only — no agent layer implemented

---

## 1. Files changed/created

| File | Purpose |
|------|---------|
| `knowledge/__init__.py` | Phase B knowledge package entry |
| `knowledge/metadata_contract.py` | Versioned provenance/metadata contract (structured, semantic, formula) |
| `knowledge/calculation.py` | Deterministic formula evaluation (no LLM numerics) |
| `knowledge/api_interfaces.py` | Protocol interfaces for future agents |
| `persistence/knowledge_pipeline.py` | Gold `gold/tables/` → PostgreSQL (OEL + chemicals), idempotent |
| `persistence/formula_registry.py` | Gold `gold/formulas/` → formula registry |
| `persistence/semantic_store.py` | Semantic corpus sync + Gemini embedding + Persian search |
| `retrieval/embeddings.py` | Vertex AI `gemini-embedding-001` implementation |
| `database/models.py` | Extended ORM: provenance columns, `KnowledgeSyncRun`, formula registry fields |
| `database/migrations/versions/005_phase_b_knowledge.py` | Alembic migration for Phase B schema |
| `scripts/run_phase_b_knowledge.py` | Orchestrator: sync tables, formulas, semantic, optional embed |
| `tests/test_phase_b_knowledge.py` | Phase B test suite (unit + DB + GCP integration) |
| `docs/knowledge/METADATA_CONTRACT.md` | Human-readable metadata/provenance contract |

---

## 2. Database changes

### Migration `005_phase_b_knowledge`

**New table:** `knowledge_sync_runs` — audit log for sync operations

**Extended tables:**

| Table | New columns / constraints |
|-------|---------------------------|
| `oel_chemical_limits` | `source_row_key`, `source_table_id_str`, `validation_status`, `gold_artifact_path`, `gold_version`, `knowledge_metadata`, `accepted_values`; **UNIQUE** `(chemical_id, source_row_key)` |
| `chemical_registry` | `aliases` (JSONB multilingual), `validation_status`, `gold_artifact_path` |
| `document_chunks` | `chunk_id`, `printed_page_number`, `section_id`, `section_title`, `validation_status`, `embedding_model/version/dimension`, `content_version`, `provenance`, `gold_artifact_path`; **UNIQUE** `(document_id, chunk_id)` |
| `formulas` | `stable_formula_id` (unique), `persian_name`, `domain`, `validation_status`, `formula_version`, `source_reference`, `semantics`, `applicability_conditions`, `gold_artifact_path`, `knowledge_metadata` |

**Indexes:** `ix_oel_source_row_key`, `ix_document_chunks_chunk_id`, `ix_formulas_stable_formula_id`, `ix_formulas_domain`, `ix_knowledge_sync_runs_type`

**pgvector:** Extension confirmed. HNSW index **not** created — `gemini-embedding-001` uses **3072 dimensions**, exceeding pgvector HNSW limit (2000). Cosine search uses sequential scan until pgvector supports higher-dimensional indexes.

---

## 3. Gold → PostgreSQL

**Command:** `python scripts/run_phase_b_knowledge.py --skip-embed`

| Metric | Value |
|--------|-------|
| Tables processed | 45 |
| Tables synced | 45 |
| Tables rejected | 0 |
| Rows synced (first run) | ~338+ (subsequent runs idempotent) |
| Rows rejected (missing CAS) | 40 |
| Duplicates prevented (re-run) | 338 |
| Chemicals in registry | 437 |
| OEL limits in DB | 2,692 |
| Unresolved | 40 rows across 12 tables — `missing_cas` (review-required rows, not guessed) |

**Gates enforced:**
- `gold/candidates/` → **rejected** (never production)
- `gold_allowed=false` → **rejected**
- Only `gold/tables/` with `gold_allowed=true` promoted

---

## 4. Semantic embedding

| Metric | Value |
|--------|-------|
| Chunks processed | 1,301 |
| Chunks synced | 738 (English legacy QA as `legacy_reference`) |
| Chunks rejected | 563 (Persian `semantic_text` with `review_status=review_required`) |
| Chunks skipped | 73 |
| Embedded (production run) | 0 legacy QA pending embed in last `--skip-embed` run |
| Test embeddings | 3 (including Persian integration test) |
| Model | `gemini-embedding-001` |
| Dimension | 3072 |
| Persian retrieval test | **PASS** — query `حد مجاز مواجهه شیمیایی` retrieved accepted Persian chunk with score > 0.5 |

**Persian policy:** Persian semantic chunks preserved as-is; not translated. English `regulatory_qa.jsonl` stored separately as `legacy_reference` without overwriting Gold originals.

---

## 5. Formula registry

| Metric | Value |
|--------|-------|
| Formulas processed | 6 |
| Formulas registered | 6 |
| Variables preserved | Yes (in `variables` + `semantics` JSONB) |
| Validation status | `accepted` (Gold `status=approved`) |
| Sample calculation | `formula_240_01`: `ahv = sqrt((1/T) * sum(ahw_i² × t_i))` with inputs `{ahw_1=2, ahw_2=3, t_1=4, t_2=6}` → **PASS** (deterministic, no LLM) |

---

## 6. Metadata / provenance example

**Chain:** PDF page 55 → Gold → PostgreSQL OEL limit

```
PDF OHE6.pdf, page 55
  → evidence: cell_table_055_01_1_2 (bbox x=246.53, y=126.36)
  → Gold: gold/tables/table_055_01.json
      STEL.original_value = "³ mg/m\n20"
      STEL.normalized_value = "20"
      STEL.accepted_value = "5" (TWA field separate)
  → PostgreSQL: oel_chemical_limits.source_row_key = "table_055_01:row_1"
      knowledge_metadata.gold_artifact_path = "gold/tables/table_055_01.json"
      original_values.TWA.original_value ≠ normalized_value (unit prefix in cell)
```

---

## 7. Tests

```
python -m pytest tests/test_phase_b_knowledge.py -v
→ 14 passed

python -m pytest tests/ -q
→ 158 passed
```

| Area | Result |
|------|--------|
| Alembic migration 005 | PASS |
| Required tables exist | PASS |
| pgvector extension | PASS |
| Gold sync idempotent | PASS |
| Candidate rejection | PASS |
| Numeric integrity (original ≠ normalized) | PASS |
| Gemini embedding integration | PASS |
| Persian retrieval | PASS |
| Formula calculation | PASS |
| Provenance E2E | PASS |

---

## 8. Known issues

1. **Vector index:** No HNSW/IVFFlat index at 3072 dimensions — large-scale retrieval will be slower until pgvector upgrade or dimension reduction strategy is chosen.
2. **Persian semantic production corpus empty:** All `semantic_text.jsonl` chunks have `review_status=review_required`; they are correctly **excluded** from production until HITL accepts them. Persian RAG depends on future Gold acceptance workflow.
3. **40 OEL rows unresolved:** Missing extractable CAS in Gold rows — left unavailable rather than inferred.
4. **Legacy English QA:** 738 `regulatory_qa` chunks synced as `legacy_reference` for audit; not primary Persian retrieval path.
5. **Existing extraction/HITL pipeline:** Unchanged — Phase B is additive sync layer only.

---

## Architecture delivered

```
GOLD (tables / formulas / rag)
        │
        ├─► PostgreSQL structured (OEL, chemicals)
        ├─► PostgreSQL pgvector (semantic chunks + embeddings)
        └─► Formula registry (deterministic calc ready)
                │
         Knowledge Layer (Phase B)
                │
         [Future Agent Layer — NOT implemented]
```

**Stop point:** Phase B complete. Agentic query layer not started per specification.
