# Semantic Retrieval Validation — FINAL Report

**Date:** 2026-08-09  
**Scope:** Persian semantic retrieval layer only (no agents)

---

## A. Files changed

| File | Purpose |
|------|---------|
| `retrieval/semantic_retrieval.py` | Production scope constants + `SemanticRetrievalResult` contract |
| `retrieval/semantic_eval_set.py` | 10 grounded Persian evaluation queries |
| `persistence/semantic_store.py` | Hardened `search_persian_semantic()` with enforced filters + full metadata |
| `scripts/evaluate_semantic_retrieval.py` | Top-K evaluation + MRR report generator |
| `scripts/test_persian_retrieval.py` | Developer CLI for ad-hoc Persian queries |
| `tests/test_semantic_retrieval.py` | Automated retrieval validation tests |
| `docs/reports/semantic_retrieval_eval.json` | Machine-readable evaluation output |
| `docs/reports/semantic_retrieval_eval.md` | Human-readable evaluation summary |

---

## B. Retrieval implementation

`search_persian_semantic(session, query, limit=5)` in `persistence/semantic_store.py`:

1. **Persian query** embedded via `EmbeddingService` → `gemini-embedding-001` @ **3072** dims (same as stored chunks).
2. **pgvector cosine distance** (`<=>`) ordered ascending; score = `1 - distance`.
3. **Hard SQL filters** (callers cannot override):
   - `source_type = 'semantic_text'`
   - `validation_status = 'accepted'`
   - `embedding IS NOT NULL`
   - `language = 'fa'`
4. Returns `SemanticRetrievalResult.to_dict()` with full metadata + provenance.
5. **Content returned verbatim** from PostgreSQL — no numeric rewriting.

Legacy evidence / English QA / row_knowledge rows are excluded by `source_type` filter.

---

## C. Data verification

| Metric | Value |
|--------|-------|
| Accepted semantic chunks (`source_type=semantic_text`, `validation_status=accepted`, `language=fa`) | **563** |
| Embedded semantic chunks | **563** |
| Embedding model | `gemini-embedding-001` |
| Embedding dimension | **3072** |
| Similarity metric | cosine distance (`<=>`) |
| Index | **None** (sequential scan) — 3072 dims exceeds pgvector HNSW 2000-dim limit |

---

## D. Evaluation (10 Persian queries)

| Metric | Result |
|--------|--------|
| Relevant in Top-1 | **6/10** |
| Relevant in Top-3 | **8/10** |
| Relevant in Top-5 | **10/10** |
| MRR | **0.75** |
| Precision@1 | 0.60 |
| Precision@3 | 0.80 |
| Precision@5 | 1.00 |

**Failures in Top-5:** none.

Queries missing Top-1 (still found in Top-3/5): STEL definition, individual sensitivity, exposure assessment basis.

---

## E. Example retrievals

### Query 1 — OEL definition
**Query:** `حدود مجاز مواجهه شغلی (OEL) چیست و به چه چیزی اشاره دارد؟`  
**Top-1:** `semantic_023_01` (score 0.83, page 23, section «تعریف حدود مجاز مواجهۀ شغلی»)

### Query 2 — Introduction
**Query:** `مقدمه فصل حدود مجاز مواجهه با عوامل شیمیایی چه هدفی دارد؟`  
**Top-1:** `semantic_021_01` (page 21, section «مقدمه»)

### Query 9 — Noise
**Query:** `حد مجاز مواجهه شغلی با صدا و فراصوت چگونه تعریف شده است؟`  
**Top-1:** `semantic_221_01` (page 221, Persian noise/infra-sound limits)

---

## F. Failures / quality notes

| Issue | Detail |
|-------|--------|
| OCR fragmentation | Many chunks retain broken Persian spacing (expected from Gold OCR repair state) |
| Empty section titles | 522/563 chunks have no section title in Gold (retrieval still works via content) |
| Top-1 misses (4 queries) | Semantically related but not exact chunk ranked first — acceptable for RAG, not for exact citation |
| English legacy QA | Correctly excluded (`source_type != semantic_text`) |
| Numerical OEL values | **Not authoritative via semantic retrieval** — use Structured Agent + `oel_chemical_limits` |

---

## G. Tests

```bash
pytest tests/test_semantic_retrieval.py -v
→ 10 passed

pytest tests/ -q
→ 170 passed
```

---

## H. Production readiness

**Semantic Retrieval: READY WITH LIMITATIONS**

**Ready because:**
- Production scope enforced in SQL
- 563/563 Persian semantic chunks embedded
- Persian query → Gemini → pgvector pipeline verified
- Top-5 recall 100% on grounded eval set
- Full metadata + provenance returned
- Automated test suite green

**Limitations:**
- Sequential scan at 3072 dims (no HNSW index yet) — acceptable at 563 chunks, will need index strategy at scale
- OCR quality in chunk text affects answer quality
- Top-1 precision 60% — agents should use Top-K + reranking/citation, not Top-1 alone
- Exact numeric OEL questions must route to Structured Agent, not semantic retrieval

**STOP:** Agent layer not implemented.
