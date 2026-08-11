"""Build intent taxonomy datasets from canonical intent definitions."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

import yaml

from intent.eval_queries_realistic import EVAL_OVERRIDES, REALISTIC_EVAL_QUERIES

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "intent_taxonomy"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Slot schema
# ---------------------------------------------------------------------------
SLOT_SCHEMA: dict[str, Any] = {
    "version": "1.0.0",
    "slots": {
        "chemical_name": {"type": "string", "languages": ["fa", "en"], "aliases": True},
        "cas": {"type": "string", "pattern": r"^\d{2,7}-\d{2}-\d$"},
        "oel_type": {"type": "enum", "values": ["TWA", "STEL", "CEILING", "ALL"]},
        "exposure_type": {"type": "enum", "values": ["TWA", "STEL", "CEILING", "BEI", "NOISE", "VIBRATION"]},
        "concentration": {"type": "number", "unit_required": True},
        "duration": {"type": "duration", "examples": ["8h", "15min", "40h/week"]},
        "unit": {"type": "string", "examples": ["ppm", "mg/m3", "mg/m³", "dB(A)", "m/s2"]},
        "noise_level": {"type": "number", "unit": "dB"},
        "vibration_value": {"type": "number"},
        "formula_id": {"type": "string", "examples": ["formula_240_01"]},
        "formula_name": {"type": "string"},
        "variables": {"type": "object"},
        "workplace_context": {"type": "string"},
        "comparison_operator": {"type": "enum", "values": [">", "<", ">=", "<=", "=="]},
        "requested_output": {"type": "enum", "values": ["value", "explanation", "calculation", "comparison"]},
        "agent_type": {"type": "enum", "values": ["chemical", "noise", "vibration", "biological"]},
        "section_reference": {"type": "string"},
        "regulation_reference": {"type": "string"},
    },
}

# ---------------------------------------------------------------------------
# Routing priority rules
# ---------------------------------------------------------------------------
ROUTING_RULES: dict[str, Any] = {
    "version": "1.0.0",
    "priority_order": [
        "1. If numeric_safety_level >= 2 and authoritative OEL requested → STRUCTURED (never SEMANTIC-only)",
        "2. If requires_calculation → FORMULA (+ STRUCTURED for inputs when needed)",
        "3. If requires_clarification → CLARIFY (do not guess missing slots)",
        "4. If requires_multiple_agents → HYBRID with ordered agent plan",
        "5. If definition/explanation only → SEMANTIC",
        "6. If structured table empty for domain → GUARDRAIL or SEMANTIC fallback per intent",
    ],
    "numeric_authority_rules": {
        "level_0": {"authority": "none", "agents": ["semantic", "structured", "formula"]},
        "level_1": {"authority": "semantic_descriptive_only", "agents": ["semantic"]},
        "level_2": {"authority": "postgresql", "agents": ["structured"], "forbid_semantic_numeric": True},
        "level_3": {"authority": "formula_engine", "agents": ["formula", "structured"], "forbid_llm_calc": True},
        "level_4": {"authority": "postgresql+formula_engine", "agents": ["structured", "formula", "guardrail"]},
    },
    "guardrail_actions": {
        "CLARIFY": "Ask user for missing required slots",
        "REFUSE": "Explain inability; do not invent data",
        "RETURN_NO_DATA": "No matching authoritative record in PostgreSQL",
        "ROUTE_TO_HUMAN_REVIEW": "Escalate when validation_status != accepted or conflict detected",
        "ROUTE_TO_MULTIPLE_AGENTS": "Hybrid plan with explicit agent sequence",
    },
    "semantic_production_filter": {
        "source_type": "semantic_text",
        "validation_status": "accepted",
        "language": "fa",
        "embedding_required": True,
    },
    "structured_production_sources": [
        "chemical_registry",
        "oel_chemical_limits",
    ],
    "structured_schema_only_sources": [
        "noise_limits",
        "vibration_limits",
        "biological_exposure_limits",
        "regulatory_constraints",
    ],
    "formula_production_sources": ["formulas"],
}

# ---------------------------------------------------------------------------
# Intent factory helpers
# ---------------------------------------------------------------------------

def _numeric_authority(source: str, *, llm_authoritative: bool = False) -> dict[str, Any]:
    return {"source": source, "llm_authoritative": llm_authoritative}


def _intent(**kwargs: Any) -> dict[str, Any]:
    source = kwargs.pop("numeric_authority_source", "none")
    base: dict[str, Any] = {
        "required_sources": [],
        "optional_sources": [],
        "requires_calculation": False,
        "requires_structured_lookup": False,
        "requires_semantic_retrieval": False,
        "requires_multiple_agents": False,
        "requires_clarification": False,
        "risk_level": "low",
        "numeric_safety_level": 0,
        "numeric_authority": _numeric_authority(source),
        "numeric_authority_source": source,
        "citation_required": True,
        "expected_answer_language": "fa",
        "routing_rules": [],
        "required_slots": [],
        "optional_slots": [],
        "missing_slots": [],
        "supported": True,
        "hybrid_agents": [],
        "example_queries_fa": [],
        "example_queries_en": [],
        "negative_examples": [],
    }
    base.update(kwargs)
    if "numeric_authority" not in kwargs and "numeric_authority_source" in kwargs:
        base["numeric_authority"] = _numeric_authority(kwargs["numeric_authority_source"])
    return base


def build_intents() -> list[dict[str, Any]]:
    intents: list[dict[str, Any]] = []

    # --- STRUCTURED: OEL (PostgreSQL populated) ---
    oel_common = {
        "parent_category": "STRUCTURED",
        "domain": "STRUCTURED",
        "capability": "CHEMICAL_OEL",
        "agent": "structured",
        "required_sources": ["chemical_registry", "oel_chemical_limits"],
        "requires_structured_lookup": True,
        "numeric_safety_level": 2,
        "numeric_authority_source": "postgresql",
        "required_slots": ["chemical_name"],
        "optional_slots": ["cas", "oel_type", "unit"],
    }
    intents.append(_intent(
        intent_id="STRUCTURED.OEL.TWA_LOOKUP",
        **oel_common,
        description="دریافت مقدار TWA مجاز برای یک ماده شیمیایی",
        purpose="Authoritative TWA from oel_chemical_limits.twa",
        routing_rules=[
            "Match chemical via CAS or fa/en name in chemical_registry",
            "Return twa + unit + provenance from oel_chemical_limits",
            "Never use semantic chunk numeric text as authority",
        ],
        example_queries_fa=[
            "حد TWA تولوئن چقدره؟",
            "برای تولوئن TWA چنده؟",
            "TWA بنزن چند ppm است؟",
            "میانگین وزنی زمانی تولوئن؟",
            "تولوئن TWA",
            "CAS 108-88-3 TWA چنده؟",
        ],
        example_queries_en=["What is the TWA for toluene?", "TWA limit benzene ppm"],
        negative_examples=["تعریف TWA چیست؟", "TWA یعنی چی؟"],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.OEL.STEL_LOOKUP",
        **oel_common,
        description="دریافت مقدار STEL مجاز",
        purpose="Authoritative STEL from oel_chemical_limits.stel",
        routing_rules=["Return stel from PostgreSQL with provenance"],
        example_queries_fa=[
            "STEL تولوئن چنده؟",
            "حد کوتاه مدت استایرن؟",
            "STEL برای CAS 100-42-5",
            "مواجهه ۱۵ دقیقه‌ای مجاز تولوئن",
            "استایرن STEL چند ppmه؟",
        ],
        negative_examples=["STEL یعنی چی؟"],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.OEL.CEILING_LOOKUP",
        **oel_common,
        description="دریافت حد سقف (Ceiling/STEL-C)",
        purpose="Authoritative ceiling from oel_chemical_limits.ceiling",
        routing_rules=["Return ceiling from PostgreSQL with provenance"],
        example_queries_fa=[
            "سقف مواجهه تولوئن چنده؟",
            "Ceiling بنزen؟",
            "حد STEL/C برای استون",
            "سقف مجاز بنزن چنده؟",
        ],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.OEL.ALL_LIMITS_LOOKUP",
        **oel_common,
        description="دریافت TWA/STEL/Ceiling یک ماده",
        purpose="Return all OEL fields for chemical",
        routing_rules=["Return twa, stel, ceiling, unit, provenance from PostgreSQL"],
        example_queries_fa=[
            "همه حدهای مجاز تولوئن رو بگو",
            "TWA و STEL و سقف بنزن",
            "حدود مجاز مواجهه استایرن",
            "برای این ماده چه حدهایی داریم؟",
        ],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.OEL.BY_CAS",
        **{**oel_common, "required_slots": ["cas"], "optional_slots": ["oel_type"]},
        description="جستجوی حد مجاز با شماره CAS",
        purpose="Resolve chemical via CAS then return OEL",
        routing_rules=["Resolve chemical_registry by CAS; return OEL from oel_chemical_limits"],
        example_queries_fa=[
            "CAS 108-88-3 چه حدی داره؟",
            "حد مجاز 71-43-2",
            "برای [100-42-5] STEL چنده؟",
            "با CAS 100-42-5 حد TWA چنده؟",
        ],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.CHEMICAL.BY_NAME",
        parent_category="STRUCTURED",
        domain="STRUCTURED",
        capability="CHEMICAL_REGISTRY",
        agent="structured",
        required_sources=["chemical_registry"],
        requires_structured_lookup=True,
        description="شناسایی ماده شیمیایی با نام فارسی یا انگلیسی",
        purpose="Resolve entity for downstream OEL lookup",
        required_slots=["chemical_name"],
        optional_slots=["cas"],
        routing_rules=["Match chemical_registry by fa_name, en_name, or alias"],
        numeric_safety_level=0,
        example_queries_fa=[
            "تولوئن CAS ش چیه؟",
            "نام انگلیسی بنزaldehyde",
            "ماده بنزن در جدول هست؟",
            "شناسه CAS استایرن",
        ],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT",
        parent_category="STRUCTURED",
        domain="STRUCTURED",
        capability="CHEMICAL_REGISTRY",
        agent="structured",
        required_sources=["chemical_registry", "oel_chemical_limits"],
        requires_structured_lookup=True,
        description="وزن مولکولی ماده",
        purpose="Return molecular_weight from registry/OEL row",
        routing_rules=["Return molecular_weight from chemical_registry or oel_chemical_limits"],
        numeric_safety_level=1,
        example_queries_fa=[
            "وزن مولکولی تولوئن چنده؟",
            "MW بنزن",
            "جرم مولی استایرن",
        ],
    ))

    intents.append(_intent(
        intent_id="STRUCTURED.OEL.PROVENANCE",
        parent_category="STRUCTURED",
        domain="STRUCTURED",
        capability="CHEMICAL_OEL",
        agent="structured",
        required_sources=["oel_chemical_limits"],
        requires_structured_lookup=True,
        description="منبع و صفحه مقدار حد مجاز",
        purpose="Return page/table/cell provenance",
        routing_rules=["Return provenance JSONB from oel_chemical_limits"],
        citation_required=True,
        example_queries_fa=[
            "این TWA از کدوم صفحه اومده؟",
            "منبع حد مجاز تولوئن کجاست؟",
            "شماره جدول حد بنزن",
            "صفحه PDF حد استایرن",
        ],
    ))

    # Schema-only structured (NOT populated — fallback semantic)
    for iid, cap, desc, fa_examples in [
        (
            "STRUCTURED.NOISE.LIMIT_LOOKUP",
            "NOISE",
            "حد مجاز صدا (SQL — schema only, use semantic fallback)",
            ["حد LAeq مجاز چنده؟", "حد مجاز ۸ ساعت صدا", "dB مجاز مواجهه"],
        ),
        (
            "STRUCTURED.VIBRATION.LIMIT_LOOKUP",
            "VIBRATION",
            "حد مجاز ارتعاش (SQL — schema only)",
            ["حد A(8) ارتعاش مجاز", "حد مواجهه لرزش دست", "حد vibration daily exposure"],
        ),
        (
            "STRUCTURED.BIOLOGICAL.BEI_LOOKUP",
            "BIOLOGICAL",
            "شاخص زیستی BEI (SQL — schema only)",
            ["BEI تولuئن", "شاخص بیولوژیک بنزن", "Biological Exposure Index استایرن"],
        ),
    ]:
        intents.append(_intent(
            intent_id=iid,
            parent_category="STRUCTURED",
            domain="STRUCTURED",
            capability=cap,
            agent="structured",
            required_sources=[f"{cap.lower()}_limits"] if cap != "BIOLOGICAL" else ["biological_exposure_limits"],
            requires_structured_lookup=True,
            supported=False,
            fallback_agent="semantic",
            fallback_note="Table exists but not populated; route to semantic_text until sync exists",
            numeric_safety_level=2,
            numeric_authority_source="postgresql",
            description=desc,
            purpose=f"Structured lookup when table populated; else semantic fallback",
            routing_rules=["If table empty → GUARDRAIL.RETURN_NO_DATA or SEMANTIC fallback per policy"],
            example_queries_fa=fa_examples,
        ))

    # --- SEMANTIC ---
    sem_base = {
        "parent_category": "SEMANTIC",
        "domain": "SEMANTIC",
        "agent": "semantic",
        "required_sources": ["document_chunks"],
        "requires_semantic_retrieval": True,
        "numeric_safety_level": 0,
        "numeric_authority_source": "semantic_descriptive_only",
        "routing_rules": [
            "search_persian_semantic with production filters",
            "Never authoritative for level-2+ numeric OEL",
        ],
    }

    semantic_intents = [
        ("SEMANTIC.DEFINITION.OEL", "DEFINITION", "تعریف حد مجاز مواجهه شغلی", [
            "حد مجاز مواجهه شغلی یعنی چی؟", "OEL چیست؟", "تعریف OELs",
        ]),
        ("SEMANTIC.DEFINITION.TWA", "DEFINITION", "تعریف TWA", [
            "TWA یعنی چی؟", "میانگین وزنی زمانی چیه؟", "تعریف TWA در بهداشت حرفه‌ای",
        ]),
        ("SEMANTIC.DEFINITION.STEL", "DEFINITION", "تعریف STEL", [
            "STEL یعنی چی؟", "مواجهه کوتاه مدت یعنی چه؟",
        ]),
        ("SEMANTIC.DEFINITION.CEILING", "DEFINITION", "تعریف حد سقف", [
            "Ceiling یعنی چی؟", "حد سقف مواجهه چیست؟",
        ]),
        ("SEMANTIC.DEFINITION.BEI", "DEFINITION", "تعریف BEI", [
            "BEI چیست؟", "شاخص زیستی مواجهه یعنی چی؟",
        ]),
        ("SEMANTIC.DEFINITION.NOISE", "DEFINITION", "تعریف حد صدا", [
            "LAeq چیست؟", "حد مجاز مواجهه صدا یعنی چی؟",
        ]),
        ("SEMANTIC.DEFINITION.VIBRATION", "DEFINITION", "تعریف ارتعاش", [
            "A(8) در ارتعاش یعنی چی؟", "حد مواجهه ارتعاش چیست؟",
        ]),
        ("SEMANTIC.EXPLANATION.CONCEPT", "EXPLANATION", "توضیح مفهومی", [
            "تفاوت TWA و STEL چیه؟", "چرا حد مجاز ۸ ساعته مهم است؟",
        ]),
        ("SEMANTIC.EXPLANATION.HEALTH", "EXPLANATION", "اثرات سلامتی", [
            "مواجهه به تولوئن چه اثراتی دارد؟", "حساسیت فردی در مواجهه شیمیایی",
        ]),
        ("SEMANTIC.EXPLANATION.EXPOSURE", "EXPLANATION", "ارزیابی مواجهه", [
            "چطور مواجهه شغلی ارزیابی می‌شود؟", "منابع تعیین حدود مجاز",
        ]),
        ("SEMANTIC.REGULATION.SCOPE", "REGULATION", "حوزه کاربرد", [
            "این حدود برای محیط شهری هم هست؟", "کاربرد حد مجاز در محیط کار",
        ]),
        ("SEMANTIC.REGULATION.PROHIBITION", "REGULATION", "ممنوعیت‌ها", [
            "آیا می‌شود از حد مجاز برای تشخیص بیماری استفاده کرد؟",
        ]),
        ("SEMANTIC.REGULATION.RECOMMENDATION", "REGULATION", "توصیه‌ها", [
            "توصیه برای کنترل غلظت آلاینده‌ها", "اقدامات کنترلی پیشنهادی",
        ]),
        ("SEMANTIC.CONTEXT.SECTION", "CONTEXT", "خلاصه بخش", [
            "خلاصه مقدمه فصل عوامل شیمیایی", "بخش عوامل فیزیکی چی میگه؟",
        ]),
        ("SEMANTIC.SYMBOL.EXPLANATION", "CONTEXT", "توضیح نمادها", [
            "نماد OTO یعنی چی؟", "ستون نمادها در جدول OEL",
        ]),
    ]
    for iid, cap, desc, fa_ex in semantic_intents:
        fa = list(fa_ex)
        while len(fa) < 3:
            fa.append(f"{desc} — توضیح بیشتر")
        intents.append(_intent(
            intent_id=iid,
            capability=cap,
            description=desc,
            purpose=f"Persian semantic retrieval: {desc}",
            example_queries_fa=fa,
            **sem_base,
        ))

    # --- FORMULA ---
    intents.append(_intent(
        intent_id="FORMULA.LOOKUP.BY_ID",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_REGISTRY",
        agent="formula",
        required_sources=["formulas"],
        description="یافتن فرمول با شناسه",
        purpose="Return formula metadata from formulas table",
        required_slots=["formula_id"],
        routing_rules=["Lookup formulas table by formula_id where validation_status=accepted"],
        example_queries_fa=[
            "فرمول formula_240_01 چیه؟",
            "رابطه ۲ صفحه ۲۴۰",
            "فرمول ahv ارتعاش کجاست؟",
        ],
    ))
    intents.append(_intent(
        intent_id="FORMULA.LOOKUP.BY_DOMAIN",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_REGISTRY",
        agent="formula",
        required_sources=["formulas"],
        description="فرمول‌های یک حوزه",
        purpose="Filter formulas.domain",
        optional_slots=["formula_name"],
        routing_rules=["Filter formulas by domain and validation_status=accepted"],
        example_queries_fa=[
            "فرمول‌های محاسبه ارتعاش",
            "فرمول exposure calculation",
            "لیست فرمول‌های صوتی",
        ],
    ))
    intents.append(_intent(
        intent_id="FORMULA.VARIABLE.EXPLANATION",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_REGISTRY",
        agent="formula",
        required_sources=["formulas"],
        requires_semantic_retrieval=False,
        description="توضیح متغیرهای فرمول",
        purpose="Return variables JSONB + semantics",
        required_slots=["formula_id"],
        optional_slots=["variables"],
        routing_rules=["Return variables from formulas.variables JSONB"],
        example_queries_fa=[
            "متغیر ahw در فرمول ارتعاش یعنی چی؟",
            "T در رابطه ۲ چیست؟",
            "متغیرهای فرمول ahv",
        ],
    ))
    intents.append(_intent(
        intent_id="FORMULA.CALCULATION.VIBRATION_AHV",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_CALCULATION",
        agent="formula",
        required_sources=["formulas"],
        requires_calculation=True,
        numeric_safety_level=3,
        numeric_authority_source="formula_engine",
        description="محاسبه ahv ارتعاش روزانه",
        purpose="Deterministic evaluate_vibration_daily_exposure",
        required_slots=["variables"],
        optional_slots=["formula_id"],
        routing_rules=["Use formula_engine only; LLM must not compute result"],
        example_queries_fa=[
            "با ahw1=2, t1=4, ahw2=3, t2=6 محاسبه ahv",
            "فرمول ارتعاش را با این مقادیر حساب کن",
            "ahv را با دو بازه زمانی محاسبه کن",
        ],
    ))
    intents.append(_intent(
        intent_id="FORMULA.CALCULATION.UNSUPPORTED",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_CALCULATION",
        agent="guardrail",
        required_sources=["formulas"],
        requires_calculation=True,
        supported=True,
        description="محاسبه فرمول بدون موتور deterministic",
        purpose="Refuse when no deterministic evaluator",
        routing_rules=["Return GUARDRAIL.UNSUPPORTED_CALCULATION"],
        example_queries_fa=[
            "این فرمول را محاسبه کن: ...",
            "نتیجه رابطه ۳ را بده",
            "فرمول صدا را حساب کن",
        ],
    ))
    intents.append(_intent(
        intent_id="FORMULA.INTERPRETATION",
        parent_category="FORMULA",
        domain="FORMULA",
        capability="FORMULA_INTERPRETATION",
        agent="hybrid",
        required_sources=["formulas", "document_chunks"],
        requires_semantic_retrieval=True,
        requires_multiple_agents=True,
        hybrid_agents=["formula", "semantic"],
        description="تفسیر نتیجه محاسبه",
        purpose="Explain deterministic result with semantic context",
        routing_rules=["Use formula result + semantic retrieval for interpretation"],
        example_queries_fa=[
            "این عدد ahv یعنی چی؟",
            "نتیجه از حد مجاز بیشتره؟ توضیح بده",
            "تفسیر نتیجه محاسبه ارتعاش",
        ],
    ))

    # --- HYBRID ---
    hybrid_specs = [
        ("HYBRID.LOOKUP_AND_EXPLAIN", ["structured", "semantic"], 2, [
            "حد مجاز تولوئن چقدره و TWA یعنی چی؟",
            "STEL بنزن چنده و چه مفهومی دارد؟",
            "TWA استایرن چنده و یعنی چی؟",
        ]),
        ("HYBRID.LOOKUP_AND_CALCULATE", ["structured", "formula"], 3, [
            "مواجهه ۲ ppm تولوئن — حد مجازش چنده و چند برابر است؟",
            "غلظت ۱.۵ ppm بنزن نسبت به TWA",
            "نسبت مواجهه به حد مجاز تولوئن",
        ]),
        ("HYBRID.LOOKUP_COMPARE_EXPLAIN", ["structured", "formula", "semantic"], 4, [
            "مواجهه ۲ ppm بود، حد مجازش چنده، بیشتره؟ توضیح بده",
            "ppm 3 تولوئن — از حد بیشتره؟ چرا؟",
            "مقایسه مواجهه با OEL و توضیح",
        ]),
        ("HYBRID.FORMULA_AND_EXPLAIN", ["formula", "semantic"], 1, [
            "فرمول دوز صدا چیست و چه مفهومی دارد؟",
            "فرمول ahv و مفهومش",
            "رابطه ۲ و توضیح متغیرها",
        ]),
        ("HYBRID.MULTI_SOURCE", ["structured", "semantic", "formula"], 3, [
            "حد تولوئن، تعریف TWA، و فرمول مرتبط را بگو",
            "OEL بنزن + توضیح + فرمول",
            "تحلیل کامل مواجهه تولuئن",
        ]),
    ]
    for iid, agents, nlevel, fa_ex in hybrid_specs:
        intents.append(_intent(
            intent_id=iid,
            parent_category="HYBRID",
            domain="HYBRID",
            capability="MULTI_AGENT",
            agent="hybrid",
            required_sources=["chemical_registry", "oel_chemical_limits", "document_chunks", "formulas"],
            requires_structured_lookup=True,
            requires_semantic_retrieval=True,
            requires_calculation=nlevel >= 3,
            requires_multiple_agents=True,
            hybrid_agents=agents,
            numeric_safety_level=nlevel,
            numeric_authority_source="postgresql" if nlevel >= 2 else "none",
            description=f"Hybrid: {' + '.join(agents)}",
            purpose="Multi-step agent plan",
            routing_rules=[f"Execute agents in order: {' → '.join(agents)}"],
            example_queries_fa=fa_ex,
        ))

    # --- CLARIFY ---
    clarify_specs = [
        ("CLARIFY.MISSING_CHEMICAL", ["chemical_name", "cas"], [
            "حد مجازش چقدره؟", "برای این ماده چی داریم؟", "STEL چنده؟",
        ]),
        ("CLARIFY.MISSING_LIMIT_TYPE", ["oel_type"], [
            "حد مجاز تولوئن؟", "برای بنزن کدوم حد؟", "TWA یا STEL؟",
        ]),
        ("CLARIFY.MISSING_EXPOSURE_VALUE", ["concentration"], [
            "آیا از حد مجاز بیشتره؟", "این مقدار خطرناکه؟",
        ]),
        ("CLARIFY.MISSING_DURATION", ["duration"], [
            "اگر ۱۰ ساعت در معرضش باشم؟", "برای ۱۵ دقیقه مجازه؟",
        ]),
        ("CLARIFY.MISSING_AGENT", ["agent_type"], [
            "حد مجازش چنده؟", "چقدر میشه؟",
        ]),
    ]
    for iid, missing, fa_ex in clarify_specs:
        fa = list(fa_ex)
        while len(fa) < 3:
            fa.append(f"سؤال مبهم — نیاز به {missing[0]}")
        intents.append(_intent(
            intent_id=iid,
            parent_category="CLARIFY",
            domain="CLARIFY",
            capability="SLOT_RECOVERY",
            agent="clarify",
            requires_clarification=True,
            missing_slots=missing,
            citation_required=False,
            description=f"نیاز به تکمیل: {', '.join(missing)}",
            purpose="Ask user; do not guess",
            routing_rules=["Return clarification prompt in Persian", "Do not route to structured until slots filled"],
            example_queries_fa=fa,
        ))

    # --- GUARDRAIL ---
    guard_specs = [
        ("GUARDRAIL.NO_DATA", "داده authoritative موجود نیست", ["اطلاعات ماده XYZ", "فرمول ناموجود"]),
        ("GUARDRAIL.UNSUPPORTED_CALCULATION", "محاسبه پشتیبانی نمی‌شود", ["فرمول X را حساب کن"]),
        ("GUARDRAIL.PROFESSIONAL_JUDGMENT", "نیاز به قضاوت حرفه‌ای", ["آیا کارگر بیمار است؟", "آیا باید اخراج شود؟"]),
        ("GUARDRAIL.UNSAFE_EXTRAPOLATION", "استقراء ناامن", ["برای ۲۰ ساعت مواجهه حدش چنده؟"]),
        ("GUARDRAIL.INVALID_INPUT", "ورودی نامعتبر", ["CAS abc-def", "ppm بدون ماده"]),
        ("GUARDRAIL.CONFLICTING_DATA", "تعارض منابع", ["چرا دو عدد مختلف دارید؟"]),
    ]
    for iid, desc, fa_ex in guard_specs:
        fa = list(fa_ex)
        while len(fa) < 3:
            fa.append(f"سؤال guardrail: {desc}")
        intents.append(_intent(
            intent_id=iid,
            parent_category="GUARDRAIL",
            domain="GUARDRAIL",
            capability="SAFETY",
            agent="guardrail",
            risk_level="high",
            citation_required=False,
            description=desc,
            purpose="Safe refusal or escalation",
            routing_rules=["REFUSE or ROUTE_TO_HUMAN_REVIEW"],
            example_queries_fa=fa,
        ))

    # --- CONVERSATION follow-up ---
    conv_specs = [
        (
            "CONVERSATION.FOLLOWUP_OEL",
            "OEL",
            ["structured", "semantic"],
            ["اون ماده", "همون تولوئن STELش چنده؟", "STELش چی بود؟"],
        ),
        (
            "CONVERSATION.FOLLOWUP_EXPOSURE",
            "EXPOSURE",
            ["structured", "formula"],
            ["اگر ۱۰ ساعت باشه چی؟", "برای ۱۵ دقیقه چطور؟", "با همون غلظت دوباره حساب کن"],
        ),
        (
            "CONVERSATION.FOLLOWUP_FORMULA",
            "FORMULA",
            ["formula", "semantic"],
            ["متغیر T چی بود؟", "همون فرمول را با t=2 حساب کن", "دوباره ahv را حساب کن"],
        ),
    ]
    for iid, cap, agents, fa_ex in conv_specs:
        intents.append(_intent(
            intent_id=iid,
            parent_category="CONVERSATION",
            domain="CONVERSATION",
            capability=cap,
            agent="hybrid",
            requires_multiple_agents=True,
            hybrid_agents=agents,
            description="ادامه گفتگو با زمینه قبلی",
            purpose="Inherit slots from conversation_state",
            optional_slots=["chemical_name", "cas", "formula_id", "duration"],
            routing_rules=[
                "Load conversation_state; merge inherited slots",
                "Re-confirm critical slots (chemical_name, cas) if ambiguous",
            ],
            example_queries_fa=fa_ex,
        ))

    return intents


# Persian query variations for dataset expansion
COLLOQUIAL_PREFIXES = ["", "لطفاً ", "میشه ", "بگو ", "بفرما "]
TYPO_MAP = {"تولوئن": "تلوئن", "بenzene": "benzen", "مواجهه": "مواجه"}


def expand_persian_variants(query: str, rng: random.Random) -> list[str]:
    variants = [query]
    if rng.random() < 0.3:
        variants.append(COLLOQUIAL_PREFIXES[rng.randint(0, len(COLLOQUIAL_PREFIXES) - 1)] + query)
    for src, dst in TYPO_MAP.items():
        if src in query:
            variants.append(query.replace(src, dst))
    # Persian digits
    if any(c.isdigit() for c in query):
        fa_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
        variants.append(query.translate(fa_digits))
    return list(dict.fromkeys(variants))


def write_examples(intents: list[dict[str, Any]], rng: random.Random) -> tuple[int, int]:
    fa_path = OUT / "intent_examples_fa.jsonl"
    en_path = OUT / "intent_examples_en.jsonl"
    neg_path = OUT / "intent_negative_examples.jsonl"
    fa_count = en_count = 0

    with fa_path.open("w", encoding="utf-8") as fa_f, en_path.open("w", encoding="utf-8") as en_f, neg_path.open("w", encoding="utf-8") as neg_f:
        for intent in intents:
            iid = intent["intent_id"]
            for q in intent.get("example_queries_fa", []):
                for v in expand_persian_variants(q, rng):
                    fa_f.write(json.dumps({"intent_id": iid, "query": v, "language": "fa"}, ensure_ascii=False) + "\n")
                    fa_count += 1
            for q in intent.get("example_queries_en", []):
                en_f.write(json.dumps({"intent_id": iid, "query": q, "language": "en"}, ensure_ascii=False) + "\n")
                en_count += 1
            for q in intent.get("negative_examples", []):
                neg_f.write(json.dumps({"intent_id": iid, "query": q, "should_not_route": True}, ensure_ascii=False) + "\n")
    return fa_count, en_count


def write_eval_set(intents: list[dict[str, Any]], rng: random.Random) -> int:
    """Build eval set from realistic domain queries only — no synthetic filler."""
    eval_path = OUT / "intent_eval_set.jsonl"
    intent_map = {i["intent_id"]: i for i in intents}
    eval_queries: list[dict[str, Any]] = []

    for iid, query in REALISTIC_EVAL_QUERIES:
        if iid not in intent_map:
            raise ValueError(f"eval query references unknown intent: {iid}")
        intent = intent_map[iid]
        override = EVAL_OVERRIDES.get(iid, {})

        if intent["agent"] == "hybrid":
            default_agents = intent.get("hybrid_agents", ["hybrid"])
        else:
            default_agents = [intent["agent"]]

        eval_queries.append({
            "query": query,
            "expected_intents": [iid],
            "expected_agents": override.get("expected_agents", default_agents),
            "requires_clarification": override.get(
                "requires_clarification", intent.get("requires_clarification", False)
            ),
            "numeric_authority": override.get(
                "numeric_authority", intent.get("numeric_authority_source", "none")
            ),
            "numeric_safety_level": override.get(
                "numeric_safety_level", intent.get("numeric_safety_level", 0)
            ),
        })

    # Deduplicate by query text (preserve first occurrence)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in eval_queries:
        if row["query"] in seen:
            continue
        seen.add(row["query"])
        unique.append(row)

    with eval_path.open("w", encoding="utf-8") as f:
        for row in unique:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(unique)


def main() -> None:
    rng = random.Random(42)
    intents = build_intents()

    taxonomy = {
        "version": "1.0.0",
        "language_primary": "fa",
        "document": "OHE6 — OHSE Document Intelligence",
        "supported_structured_sources": ["chemical_registry", "oel_chemical_limits"],
        "schema_only_structured_sources": [
            "noise_limits",
            "vibration_limits",
            "biological_exposure_limits",
            "regulatory_constraints",
        ],
        "semantic_production_filter": ROUTING_RULES["semantic_production_filter"],
        "intents": intents,
    }
    (OUT / "intent_taxonomy.yaml").write_text(
        yaml.dump(taxonomy, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    (OUT / "intent_slots.yaml").write_text(
        yaml.dump(SLOT_SCHEMA, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (OUT / "intent_routing_rules.yaml").write_text(
        yaml.dump(ROUTING_RULES, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    fa_n, en_n = write_examples(intents, rng)
    eval_n = write_eval_set(intents, rng)

    print(json.dumps({
        "intents": len(intents),
        "fa_examples": fa_n,
        "en_examples": en_n,
        "eval_queries": eval_n,
        "output_dir": str(OUT),
    }, indent=2))


if __name__ == "__main__":
    main()
