# Intent Taxonomy — Final Report

**Project:** OHSE Document Intelligence (OHE6.pdf)  
**Date:** 2026-08-09  
**Status:** **READY WITH LIMITATIONS**

---

## Executive Summary

A production intent taxonomy has been designed and materialized for the Persian-first OHSE Document Intelligence system. The taxonomy reflects **actual database capabilities** discovered during schema and pipeline inspection:

| Data Source | PostgreSQL Status | Agent Authority |
|-------------|-------------------|-----------------|
| `chemical_registry` + `oel_chemical_limits` | ✅ Populated (~437 chemicals, ~2692 OEL rows) | Structured Agent |
| `document_chunks` (`semantic_text`, `fa`, embedded) | ✅ 563 chunks | Semantic RAG Agent |
| `formulas` | ✅ 6 approved formulas | Formula Agent |
| `noise_limits`, `vibration_limits`, `biological_exposure_limits`, `regulatory_constraints` | ❌ Schema only, no sync | Semantic fallback (not authoritative for numbers) |

**Deliverables location:** `data/intent_taxonomy/`  
**Validation:** `tests/test_intent_taxonomy.py` — **19/19 passed**

---

## 1. Taxonomy Statistics

| Metric | Value |
|--------|-------|
| Total intents | **51** |
| Persian training examples (JSONL) | **258** |
| English training examples (JSONL) | **2** (robustness only) |
| Evaluation queries | **138** (realistic OHE6 domain — no synthetic filler) |
| Hybrid intents | **9** |
| Clarification intents | **5** |
| Guardrail intents | **6** |
| Safety-critical (numeric level ≥ 4) | **1** |
| Level 2+ numeric intents | **13** |
| Conversation follow-up intents | **3** |

### Agent Distribution

| Agent | Intent Count |
|-------|-------------|
| semantic | 15 |
| structured | 11 |
| hybrid | 9 |
| guardrail | 7 |
| formula | 4 |
| clarify | 5 |

---

## 2. Hierarchical Taxonomy

```
STRUCTURED (11)
├── CHEMICAL_OEL
│   ├── STRUCTURED.OEL.TWA_LOOKUP
│   ├── STRUCTURED.OEL.STEL_LOOKUP
│   ├── STRUCTURED.OEL.CEILING_LOOKUP
│   ├── STRUCTURED.OEL.ALL_LIMITS_LOOKUP
│   ├── STRUCTURED.OEL.BY_CAS
│   └── STRUCTURED.OEL.PROVENANCE
├── CHEMICAL_REGISTRY
│   ├── STRUCTURED.CHEMICAL.BY_NAME
│   └── STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT
└── [SCHEMA-ONLY — supported=false, fallback=semantic]
    ├── STRUCTURED.NOISE.LIMIT_LOOKUP
    ├── STRUCTURED.VIBRATION.LIMIT_LOOKUP
    └── STRUCTURED.BIOLOGICAL.BEI_LOOKUP

SEMANTIC (15)
├── DEFINITION
│   ├── SEMANTIC.DEFINITION.OEL / .TWA / .STEL / .CEILING
│   ├── SEMANTIC.DEFINITION.BEI / .NOISE / .VIBRATION
├── EXPLANATION
│   ├── SEMANTIC.EXPLANATION.CONCEPT / .HEALTH / .EXPOSURE
├── REGULATION
│   ├── SEMANTIC.REGULATION.SCOPE / .PROHIBITION / .RECOMMENDATION
└── CONTEXT
    ├── SEMANTIC.CONTEXT.SECTION
    └── SEMANTIC.SYMBOL.EXPLANATION

FORMULA (6)
├── FORMULA.LOOKUP.BY_ID / .BY_DOMAIN
├── FORMULA.VARIABLE.EXPLANATION
├── FORMULA.CALCULATION.VIBRATION_AHV  ← only deterministic evaluator
├── FORMULA.CALCULATION.UNSUPPORTED    → guardrail
└── FORMULA.INTERPRETATION             → hybrid (formula + semantic)

HYBRID (5 + 3 conversation = 8 routing hybrids)
├── HYBRID.LOOKUP_AND_EXPLAIN          (structured + semantic)
├── HYBRID.LOOKUP_AND_CALCULATE        (structured + formula)
├── HYBRID.LOOKUP_COMPARE_EXPLAIN      (structured + formula + semantic) [level 4]
├── HYBRID.FORMULA_AND_EXPLAIN         (formula + semantic)
└── HYBRID.MULTI_SOURCE                (structured + semantic + formula)

CLARIFY (5)
├── CLARIFY.MISSING_CHEMICAL
├── CLARIFY.MISSING_LIMIT_TYPE
├── CLARIFY.MISSING_EXPOSURE_VALUE
├── CLARIFY.MISSING_DURATION
└── CLARIFY.MISSING_AGENT

GUARDRAIL (6)
├── GUARDRAIL.NO_DATA
├── GUARDRAIL.UNSUPPORTED_CALCULATION
├── GUARDRAIL.PROFESSIONAL_JUDGMENT
├── GUARDRAIL.UNSAFE_EXTRAPOLATION
├── GUARDRAIL.INVALID_INPUT
└── GUARDRAIL.CONFLICTING_DATA

CONVERSATION (3)
├── CONVERSATION.FOLLOWUP_OEL
├── CONVERSATION.FOLLOWUP_EXPOSURE
└── CONVERSATION.FOLLOWUP_FORMULA
```

---

## 3. Intent Table (Summary)

| Intent | Agent | Sources | Calc | Struct | Semantic | Hybrid | Risk | Numeric Level |
|--------|-------|---------|------|--------|----------|--------|------|---------------|
| STRUCTURED.OEL.* | structured | chemical_registry, oel_chemical_limits | — | ✓ | — | — | low | 2 |
| STRUCTURED.CHEMICAL.* | structured | chemical_registry | — | ✓ | — | — | low | 0–1 |
| STRUCTURED.NOISE/VIBRATION/BIO | structured→semantic | schema-only tables | — | ✓* | fallback | — | low | 2 |
| SEMANTIC.* | semantic | document_chunks | — | — | ✓ | — | low | 0 |
| FORMULA.LOOKUP.* | formula | formulas | — | — | — | — | low | 0 |
| FORMULA.CALCULATION.VIBRATION_AHV | formula | formulas | ✓ | — | — | — | medium | 3 |
| FORMULA.INTERPRETATION | hybrid | formulas, document_chunks | — | — | ✓ | ✓ | low | 1 |
| HYBRID.* | hybrid | multi | varies | ✓ | ✓ | ✓ | low–critical | 1–4 |
| CLARIFY.* | clarify | — | — | — | — | — | low | 0 |
| GUARDRAIL.* | guardrail | — | — | — | — | — | high | 0 |
| CONVERSATION.* | hybrid | context+agents | varies | ✓ | ✓ | ✓ | low | 0–3 |

\* Structured lookup marked `supported=false` until Gold→Postgres sync exists for these tables.

---

## 4. Numeric Safety Classification

| Level | Description | Authority | Orchestrator Enforcement |
|-------|-------------|-----------|--------------------------|
| **0** | No authoritative numbers | none | Semantic/structured allowed |
| **1** | Descriptive numbers (MW, page refs) | semantic_descriptive_only | LLM may describe; cite source |
| **2** | Authoritative OEL lookup | **PostgreSQL** | Structured Agent ONLY; semantic forbidden for numeric answer |
| **3** | Deterministic calculation | **formula_engine** | `evaluate_vibration_daily_exposure()` etc.; LLM explains only |
| **4** | Safety-critical decision | **postgresql + formula_engine** | Hybrid plan; guardrail on missing inputs; human review if conflict |

Rules are codified in `data/intent_taxonomy/intent_routing_rules.yaml`.

---

## 5. Routing Priority

1. **Level 2+ numeric OEL** → Structured Agent (never Semantic-only)
2. **Calculation requested** → Formula Agent (+ Structured for inputs)
3. **Missing critical slots** → CLARIFY (do not guess)
4. **Multi-capability query** → HYBRID with ordered agent plan
5. **Definition/explanation only** → Semantic RAG
6. **Schema-only table empty** → GUARDRAIL.RETURN_NO_DATA or Semantic fallback per intent policy

---

## 6. Slot Schema

Defined in `data/intent_taxonomy/intent_slots.yaml`:

- `chemical_name`, `cas`, `oel_type`, `exposure_type`
- `concentration`, `duration`, `unit`, `noise_level`, `vibration_value`
- `formula_id`, `formula_name`, `variables`
- `workplace_context`, `comparison_operator`, `requested_output`, `agent_type`

Each intent specifies `required_slots`, `optional_slots`, and (for CLARIFY) `missing_slots`.

---

## 7. Dataset Files

| File | Purpose |
|------|---------|
| `intent_taxonomy.yaml` | Canonical intent definitions |
| `intent_examples_fa.jsonl` | Persian training/router examples (258) |
| `intent_examples_en.jsonl` | English robustness examples (2) |
| `intent_negative_examples.jsonl` | Mis-routing guards |
| `intent_routing_rules.yaml` | Priority and numeric authority rules |
| `intent_slots.yaml` | Entity/slot schema |
| `intent_eval_set.jsonl` | Held-out eval set (150 queries) |

Regenerate: `python scripts/build_intent_taxonomy.py`

---

## 8. Evaluation Set

- **138 realistic domain queries** from `intent/eval_queries_realistic.py` — symbols, OEL lookups, BEI, biological monitoring, clarify/guardrail/hybrid edge cases
- **No placeholder queries** (validation rejects `سؤال ارزیابی ...` patterns)
- **Separate from training examples** (validated by automated checks)
- Each row includes: `expected_intents`, `expected_agents`, `requires_clarification`, `numeric_authority`

---

## 9. Automated Validation & Tests

`intent/taxonomy_schema.py` provides:

- `validate_taxonomy()` — duplicates, agent mapping, numeric safety, hybrid rules
- `validate_examples()` — JSONL coverage per intent
- `validate_eval_set()` — size, dedup, train/eval separation, hybrid agent checks
- `validate_routing_rules()` / `validate_slots()`
- `validate_all()` — combined gate

**Test results:** `pytest tests/test_intent_taxonomy.py` → **19 passed**

---

## 10. Identified Overlaps / Ambiguities

| Overlap | Resolution |
|---------|------------|
| OEL numeric vs OEL definition | Negative examples + numeric level 2 routing to structured |
| Noise/vibration limit lookup | Structured intent exists but `supported=false`; semantic fallback until sync |
| Formula lookup vs formula explain | Separate FORMULA.LOOKUP vs HYBRID.FORMULA_AND_EXPLAIN |
| «حد مجاز X» without limit type | CLARIFY.MISSING_LIMIT_TYPE |
| «حد مجازش» without chemical | CLARIFY.MISSING_CHEMICAL |

---

## 11. Unresolved Limitations

1. **Noise/vibration/biological/regulatory structured tables** — schema exists, no Gold sync; numeric authority for these domains cannot come from PostgreSQL yet.
2. **Only one deterministic formula evaluator** — `evaluate_vibration_daily_exposure()` (ahv); other formulas return GUARDRAIL.UNSUPPORTED_CALCULATION.
3. **English examples minimal** — by design (Persian-first production).
4. **No Query Router implementation** — taxonomy is ready as orchestrator input; router/orchestrator not built.
5. **OEL ratio/comparison formulas** — not in formula registry; HYBRID.LOOKUP_AND_CALCULATE depends on future ratio formula or structured comparison logic.
6. **Conversation state store** — CONVERSATION intents defined; session state mechanism not implemented.

---

## 12. Recommended Next Steps (Query Router / Orchestrator)

1. **Implement slot extractor** (Persian NER) using `intent_slots.yaml` required/optional slots per intent.
2. **Build classifier** trained/evaluated on `intent_examples_fa.jsonl`, validated on `intent_eval_set.jsonl`.
3. **Implement routing engine** applying `intent_routing_rules.yaml` priority order.
4. **Wire agent stubs** from `knowledge/api_interfaces.py` to structured SQL, `search_persian_semantic()`, and `evaluate_formula_by_id()`.
5. **Add conversation state** (Redis or session table) for CONVERSATION.* intents.
6. **Sync noise/vibration/biological Gold tables** → enable `supported=true` on schema-only structured intents.
7. **Register OEL ratio formula** in formula registry for exposure comparison calculations.

---

## 13. Verdict

| Criterion | Status |
|-----------|--------|
| Taxonomy files exist | ✅ |
| Persian-first examples | ✅ (258) |
| Eval set ≥ 100 | ✅ (138 realistic) |
| Routing rules defined | ✅ |
| Numeric safety enforced in schema | ✅ |
| Automated tests pass | ✅ (19/19) |
| Reflects actual DB capabilities | ✅ |
| All agent types covered | ✅ |

**INTENT TAXONOMY: READY WITH LIMITATIONS**

Limitations are documented above (schema-only structured tables, single formula evaluator, no router implementation). The taxonomy is suitable as production input for Query Router development.
