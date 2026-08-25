# OHSE Correctness & Retrieval Closure Report

Generated: 2026-08-14 (Phase F0.2 → P5 investigation cycle)

## Executive summary

The occupational exposure limit (OEL) Q&A system is **correctness-safe for production structured lookups** after closing the canonical/legacy boundary gap. Numeric answers come only from accepted canonical evidence (`canonical_evidence_v1`). Wrong-chemical rate is **0%** on the 44-case living correctness matrix; legacy-only chemicals correctly return `no_data` with reason `legacy_only_pending_promotion`.

Semantic retrieval had a **parallel-path scope bug** in `HYBRID_RERANK` (fixed; same class found in `VECTOR_LEXICAL`). Offline benchmarks at n=100 show D (70% Recall@5) vs C (67%) — **not statistically distinguishable** (two-proportion z-test p ≈ 0.65). **Production default is `VECTOR_METADATA`** (simpler, lower risk). **`HYBRID_RERANK` is the A/B canary experimental arm** until live traffic confirms a significant winner.

---

## Root cause (primary — structured)

**Silent legacy fallback in structured store** — `PostgresStructuredStore._limits_for_chemical()` returned `legacy_reference` rows when no canonical row existed, causing **119 legacy-only chemicals** to receive authoritative numeric answers from stale data.

### Fix (P0 + P1)

| Layer | Change |
|-------|--------|
| P0 | Structured store requires `validation_status=accepted` AND `gold_artifact_path=canonical_evidence_v1` |
| P1 | `validate_authoritative_oel_row()` — chemical_id / CAS mismatch → fail closed |
| P0 UX | `NoDataReason.PENDING_PROMOTION` + distinct user message for legacy-only chemicals |
| HITL | `no_data_events` table logs demand for legacy-only chemicals |

---

## Acceptance scorecard (correctness — final)

| Gate | Target | Result | Status |
|------|--------|--------|--------|
| Wrong-chemical rate (E2E matrix) | 0% | **0%** (0/44) | PASS |
| Legacy-only no_data correctness | 100% | **100%** (18/18) | PASS |
| Canonical OEL rows in DB | 349 | **349** | PASS |
| Canonical row_knowledge chunks | 349 | **349** | PASS |
| Semantic chunks embedded | 563 | **563** (0 unembedded) | PASS |
| Orphan canonical chunks | 0 | **0** (JOIN fix) | PASS |
| Duplicate limit-type invariant | 0 | **0** | PASS |
| Phase 7 transaction | committed | **committed** | PASS |

---

## D_hybrid_rerank investigation

### Ground-truth validity

| Check | Result |
|-------|--------|
| Eval cases (sample) | 30 |
| Ground-truth chunk IDs valid in active corpus | **30/30** |
| Missing from DB | **0** |

**Conclusion:** 6.7% Recall@5 was **not** stale eval IDs — it was a pipeline bug.

### Root cause (parallel-path scope divergence)

`HYBRID_RERANK` applied `page_hint` to **vector** candidates only; **lexical** candidates were unfiltered. `LexicalReranker` promoted wrong-page chunks (e.g. RET-001993: page 378 vs expected page 21).

Same bug class as P0 legacy fallback: one parallel path ignored authority/scope that the other enforced.

### Fix

- `_apply_page_scope()` — identical `_metadata_filter` on vector + lexical before merge
- Also fixed **VECTOR_LEXICAL** (same asymmetry, previously unscoped vector pool)
- Permanent audit tool: `retrieval/scope_audit.py` (`python -m retrieval.scope_audit`)

### Scope filter audit

| Filter | Vector path | Lexical path | Parity |
|--------|-------------|--------------|--------|
| `source_type=semantic_text` | SQL WHERE | index build | OK |
| `validation_status=accepted` | SQL WHERE | index build | OK |
| `language=fa` | SQL WHERE | index build | OK |
| `exclude_test_chunks` | post-retrieval | index build | OK |
| `page_hint` | post-retrieval | post-retrieval | **Fixed** |
| `chemical_id_hint` | n/a | n/a | **Intentionally out of semantic scope** (structured routing only — see `scope_audit.py`) |

Reranker operates on pre-scoped pools only — no independent metadata filter.

---

## Retrieval benchmark (post-fix)

### n=30 (initial post-fix sample)

| Mode | Recall@5 | MRR | p50 (ms) | p95 (ms) |
|------|----------|-----|----------|----------|
| D_hybrid_rerank | 53.3% | 0.444 | 75 | 98 |
| C_vector_metadata | 43.3% | 0.417 | 81 | 125 |

**Before fix (D, n=30):** Recall@5 6.7%, MRR **0.050** → after fix MRR **0.444** (ranking inside top-5 recovered).

### n=100 (expanded offline eval)

| Mode | Recall@5 | MRR | p50 (ms) | p95 (ms) |
|------|----------|-----|----------|----------|
| D_hybrid_rerank | 70.0% | 0.673 | 77 | 104 |
| C_vector_metadata | 67.0% | 0.665 | **71** | **92** |
| B_vector_lexical | 70.0% | 0.673 | 76 | 98 |
| A_vector_only | 57.0% | 0.570 | 24 | 33 |

### Statistical significance (D vs C, n=100)

Two-proportion z-test on Recall@5 (70/100 vs 67/100):

```
p̂ = 0.685
SE  = √(0.685 × 0.315 × (1/100 + 1/100)) ≈ 0.0657
z   = 0.03 / 0.0657 ≈ 0.46
p-value ≈ 0.65
```

**Conclusion:** At n=100, D and C are **not statistically distinguishable**. The 3-query gap may be noise. Offline eval alone is **insufficient evidence** to prefer the more complex mode.

**Tie-break rule:** When modes are statistically tied, **prefer the simpler mode** (`VECTOR_METADATA`) — fewer code paths, lower regression surface (this bug class originated in hybrid path complexity).

397 semantic eval cases remain available for future full runs; production A/B will converge faster on real traffic.

---

## Production mode decision

| Role | Mode | Rationale |
|------|------|-----------|
| **Production default** | `VECTOR_METADATA` | Simpler; statistically tied with D at n=100; slightly lower p50/p95 |
| **A/B canary arm** | `HYBRID_RERANK` | Experimental until live data shows significant advantage |
| **Canary fallback** (HYBRID arm only) | `VECTOR_METADATA` | On `primary_error` or `empty_with_page_hint` |

This is **not** “D wins because 70% > 67%” — that conclusion would overstate offline evidence.

### A/B canary plan (required before final lock)

Apply to **10–20% of total traffic**, split **50/50** within the canary cohort:

| Arm | Mode | Config |
|-----|------|--------|
| Control | `VECTOR_METADATA` | `SemanticAgent(retrieval_mode=VECTOR_METADATA)` |
| Treatment | `HYBRID_RERANK` | `SemanticAgent(retrieval_mode=HYBRID_RERANK, fallback_mode=VECTOR_METADATA)` |

Log per request: `retrieval_mode`, `retrieval_trace`, latency, fallback reason (if any).

#### Success / rollback criteria (define before launch)

| Criterion | Threshold | Action |
|-----------|-----------|--------|
| Wrong-chemical rate | **0%** in both arms | **Non-negotiable** — abort A/B immediately if violated |
| Fallback trigger rate (HYBRID arm) | Monitor `primary_error`, `empty_with_page_hint` | Investigate if unexpectedly high |
| Recall / MRR advantage | Significant at p < 0.05 after **7 days** or **≥500 semantic queries** per arm | Promote winner to default |
| No significant difference after window | p ≥ 0.05 | **Keep `VECTOR_METADATA` default**; leave `HYBRID_RERANK` experimental |

Also monitor: `no_data_events` (`legacy_only_pending_promotion`), structured correctness gates, p95 latency.

---

## Regression tests

| Test | Path |
|------|------|
| Page scope parity + RET-001993 class | `tests/test_hybrid_rerank_page_scope.py` |
| SemanticAgent default + canary fallback | `tests/test_semantic_agent_fallback.py` |

**Policy:** Every parallel-path scope bug → regression test + `scope_audit.py` entry.

---

## Living test suites

- `tests/test_structured_authority.py` — 44+ parametrized E2E cases
- `scripts/run_p5_benchmark.py` — post-Phase-7 correctness + retrieval gate
- `scripts/run_retrieval_benchmark.py` — retrieval-only MRR/latency

---

## Closure checklist

| Step | Status |
|------|--------|
| P0–P4 correctness core | ✅ |
| Phase 7 full sync | ✅ |
| P0 monitoring | ✅ |
| D root cause + fix + systematic audit | ✅ |
| Regression tests | ✅ |
| MRR verification | ✅ |
| n=100 retrieval eval | ✅ |
| Latency measurement | ✅ |
| Statistical significance documented | ✅ (D vs C not significant) |
| Default = C on tie (simplicity) | ✅ |
| `chemical_id_hint` scope documented | ✅ |
| A/B canary (operational) | ⏳ |
| Final default lock | ⏳ after canary |

---

## Artifacts

| Report | Path |
|--------|------|
| Retrieval benchmark JSON | `data/evaluation/p5_results/p5_retrieval_benchmark.json` |
| Scope audit | `retrieval/scope_audit.py` |
| Hybrid diagnostic | `scripts/diagnose_hybrid_rerank.py` |
| P5 benchmark | `scripts/run_p5_benchmark.py` |
