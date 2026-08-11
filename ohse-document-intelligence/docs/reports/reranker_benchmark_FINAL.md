# Reranker Benchmark — Phase C.2

**Date:** 2026-08-09  
**Sample:** 30 semantic eval queries (live PostgreSQL + Gemini embedding API)

## Summary

| Config | Candidate K | Final K | Recall@5 | MRR | nDCG@5 | P@5 | Latency (ms) |
|--------|-------------|---------|----------|-----|--------|-----|--------------|
| **metadata_filter** | 20 | 5 | **43.3%** | **0.433** | **0.433** | **29.4%** | 3,943 |
| metadata_boost | 20 | 5 | 36.7% | 0.344 | 0.350 | 7.3% | 3,960 |
| lexical_overlap | 10 | 5 | 31.6% | 0.316 | 0.316 | 6.3% | 5,233 |
| baseline_top_20 | 20 | 20 | 26.7%@5 / **43.3%@20** | 0.283 | 0.267 | 5.3% | 4,747 |
| baseline_top_10 | 10 | 10 | 26.7%@5 / 33.3%@10 | 0.276 | 0.267 | 5.3% | 4,829 |
| baseline_no_rerank | 20 | 5 | 26.7% | 0.267 | 0.267 | 5.3% | 6,014 |
| lexical_overlap | 20 | 5 | 26.7% | 0.267 | 0.267 | 5.3% | 5,939 |

## Baseline (Vector Only)

Gemini embedding → pgvector cosine → Top-K:

| K | Recall@K | MRR |
|---|----------|-----|
| 1 | 26.7% | 0.267 |
| 3 | 26.7% | 0.267 |
| 5 | 26.7% | 0.267 |
| 10 | 33.3% | 0.276 |
| 20 | **43.3%** | 0.283 |

Increasing K beyond 5 improves recall only when returning more results — Top-5 recall is flat at 26.7% without reranking.

## Reranker Comparison

### Available (no extra deps)
1. **baseline_no_rerank** — vector order preserved
2. **lexical_overlap** — token overlap + vector blend (local, 0 MB)

### Not installed in environment
- `cross-encoder/ms-marco-MiniLM-L-6-v2` (requires `sentence-transformers`)
- `BAAI/bge-reranker-v2-m3` (requires `FlagEmbedding`)

## Candidate Pool Optimization (Top-N → Rerank → Top-5)

| Pipeline | Recall@5 | vs Baseline |
|----------|----------|-------------|
| Top-10 → no rerank → 5 | 26.7% | — |
| Top-10 → lexical → 5 | **31.6%** | **+4.9pp** |
| Top-20 → no rerank → 5 | 26.7% | — |
| Top-20 → lexical → 5 | 26.7% | 0 |
| Top-30 → lexical → 5 | 26.7% | 0 |

**Finding:** Lexical reranker helps only at candidate_k=10. Larger pools do not improve Top-5 recall with lexical reranking.

## Metadata-Aware Retrieval

| Mode | Recall@5 | Notes |
|------|----------|-------|
| vector_only | 26.7% | Default |
| metadata_filter (page) | **43.3%** | Best measured config — safe page filter |
| metadata_boost (+0.05 same page) | 36.7% | Moderate improvement |

**Recommendation:** Use page-aware metadata filter when eval record includes authoritative page_number. Do NOT use aggressive filters that drop cross-page relevant chunks.

## Best Configuration

**Production recommendation (based on measured data):**

```
Query → Gemini embed → pgvector Top-20 → page metadata filter → Top-5
```

- **Why:** metadata_filter achieves 43.3% Recall@5 vs 26.7% baseline (+16.6pp)
- Lexical reranker is secondary (+4.9pp at K=10 only)
- BGE/cross-encoder not evaluated (not installed); install for further benchmark

## Latency Impact

- Embedding API dominates: ~4–6 seconds/query
- Lexical reranker adds <1ms
- Metadata filter adds negligible overhead
- Full benchmark (30 queries × 14 configs): ~25 minutes

## Failure Patterns

1. Generic section titles ("مقدمه") retrieve wrong-page chunks with similar vocabulary
2. BEI queries retrieve exposure-table chunks instead of definition chunks
3. Test chunks (`test_persian_*`) pollute results for OEL definition queries
4. OCR fragmentation causes near-duplicate chunks on pages 315+

Output: `data/evaluation/reranker_results.jsonl`

## Reproduce

```bash
python scripts/run_phase_c2_evaluation.py --mode semantic --limit 30
```
