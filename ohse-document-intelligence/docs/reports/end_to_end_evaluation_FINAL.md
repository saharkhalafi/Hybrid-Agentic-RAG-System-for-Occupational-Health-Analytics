# Phase C — End-to-End Evaluation & Implementation Report

**Date:** 2026-08-09  
**Status:** **READY WITH LIMITATIONS**

---

## 1. Architecture Implemented

```text
POST /query
    → SessionContextManager (resolve follow-ups)
    → normalize_persian_query
    → extract_slots
    → IntentClassifier (taxonomy-driven, rule-based)
    → QueryRouter (plan agents from taxonomy)
    → Structured / Semantic / Formula / Hybrid agents
    → GuardrailGate (numeric + citation authority)
    → AnswerSynthesizer (Persian, template-grounded)
    → QueryResponse + trace_id
```

## 2. Files Created

| Path | Role |
|------|------|
| `agents/session/` | Session store + context resolution |
| `agents/routing/` | Normalizer, slots, classifier, router |
| `agents/structured/` | PostgreSQL OEL store + agent |
| `agents/semantic/` | Semantic RAG agent |
| `agents/formula/` | Formula registry + calculation agent |
| `agents/hybrid/` | Multi-agent orchestration |
| `agents/guardrails/` | Authority gate |
| `agents/synthesis/` | Persian answer synthesis |
| `agents/orchestrator/` | Pipeline + trace |
| `agents/evaluation/` | Eval harness |
| `api/routers/query_router.py` | POST /query |
| `scripts/run_phase_c_evaluation.py` | Evaluation CLI |
| `tests/test_phase_c_agents.py` | 18 unit/integration tests |

## 3. Session Context

- In-memory `SessionStore` (no persistent user memory)
- Follow-up resolution: «STEL نداره؟» → «آیا Benzene STEL دارد؟»
- Explicit entities override inherited context
- Ambiguous queries without session → CLARIFY
- Full context trace in execution metadata

## 4. Guardrails

- Level 2+ numeric → PostgreSQL only (semantic numeric forbidden)
- Formula results → formula engine only
- Insufficient evidence → Persian refusal message
- CLARIFY before hallucination

## 5. API

```http
POST /query
{"session_id": "optional-uuid", "query": "حد مجاز بنزن چقدره؟"}
```

Response: `answer`, `intent`, `agents`, `citations`, `confidence`, `trace_id`, `metadata.trace`

## 6. Evaluation Metrics (Actual Run)

### Router (full eval set — 2,696 queries)

| Metric | Result |
|--------|--------|
| Intent accuracy | **82.83%** |
| Agent routing accuracy | **89.09%** |
| Clarification accuracy | **92.99%** |

Output: `data/evaluation/router_results.jsonl`

### End-to-end (100 queries, live DB)

| Metric | Result |
|--------|--------|
| Intent accuracy | **97.00%** |
| Passed | 97/100 |
| Failed | 3 |

Failures: multi-word chemical names (`acid Acetic`) and STEL duration phrase misclassification (`مواجهه ۱۵ دقیقه‌ای مجاز Acetone`).

### Tests

```bash
pytest tests/ -q --ignore=tests/test_semantic_retrieval.py
# 216 passed
```

## 7. Example End-to-End Queries

**Turn 1:** `حد مجاز بنزن چقدره؟`  
→ Intent: `STRUCTURED.OEL.TWA_LOOKUP` → PostgreSQL numeric answer + citation

**Turn 2 (same session):** `STEL نداره؟`  
→ Resolved: `آیا Benzene STEL دارد؟` → Structured STEL lookup

**Definition:** `TWA یعنی چی؟`  
→ Intent: `SEMANTIC.DEFINITION.TWA` → Persian semantic chunks

**Formula:** `با ahw1=1.5,t1=3,ahw2=2.5,t2=5 ahv را محاسبه کن`  
→ Deterministic formula engine result

## 8. Known Limitations

1. Rule-based classifier ~83% on full eval — needs ML refinement for production
2. Multi-word chemical name slot extraction weak
3. Semantic eval (Recall@K) not run in CI without GCP embedding API
4. Answer synthesis is template-based (no LLM polish) — intentionally grounded
5. Session store is in-memory (lost on restart)

## 9. Recommended Next Steps

1. Fine-tune intent classifier on `intent_examples_fa.jsonl`
2. Improve chemical NER for multi-token names
3. Add Redis session store for production persistence (TTL-scoped)
4. Run full semantic Recall@K eval with GCP credentials
5. Optional LLM synthesis layer with strict citation-only prompt

---

**PHASE C: READY WITH LIMITATIONS**
