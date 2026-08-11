# Phase C — Query Router Evaluation

**Date:** 2026-08-09  
**Dataset:** `data/retrieval_eval/retrieval_eval_master.jsonl` (2,696 records evaluated)

## Summary

| Metric | Value |
|--------|-------|
| Total queries | 2,696 |
| Intent accuracy | **82.83%** |
| Agent routing accuracy | **89.09%** |
| Clarification accuracy | **92.99%** |
| Passed (intent + agents) | 2,233 |
| Failed | 463 |
| Avg classifier latency | ~0.05 ms |

## Top Failure Patterns

1. **Multi-word chemical names** — e.g. `acid Acetic`, `choloride Allyl` not extracted → routed to `CLARIFY.MISSING_CHEMICAL` instead of structured STEL/TWA.
2. **STEL duration phrases** — `مواجهه ۱۵ دقیقه‌ای مجاز X` misclassified as `HYBRID.LOOKUP_COMPARE_EXPLAIN` due to numeric duration + «مجاز» pattern.
3. **Semantic vs structured boundary** — definition queries containing TWA token without «یعنی/تعریف» routed to structured.

## Sample Failures

See `data/evaluation/router_results.jsonl` for full failure sample and confusion pairs.

## Confusion Examples

| Expected | Predicted | Cause |
|----------|-----------|-------|
| STRUCTURED.OEL.STEL_LOOKUP | CLARIFY.MISSING_CHEMICAL | Complex English chemical tokenization |
| STRUCTURED.OEL.STEL_LOOKUP | HYBRID.LOOKUP_COMPARE_EXPLAIN | «۱۵ دقیقه» + «مجاز» heuristic |
| SEMANTIC.EXPLANATION.CONCEPT | STRUCTURED.OEL.TWA_LOOKUP | Missing definition marker in query |

## Recommendations

1. Improve slot extractor for multi-token chemical names (strip leading adjectives: acid, chloride).
2. Prioritize STEL keyword over hybrid compare when `STEL` or «۱۵ دقیقه» present without exposure value.
3. Train lightweight classifier on `intent_examples_fa.jsonl` to raise accuracy above rule-only baseline.

## Reproduce

```bash
python scripts/run_phase_c_evaluation.py --mode router
```
