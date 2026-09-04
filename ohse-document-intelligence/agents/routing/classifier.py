"""Rule-based intent classifier using production intent taxonomy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from config.settings import PROJECT_ROOT
from agents.routing.slots import CAS_PATTERN, is_valid_cas
from retrieval.formula_inventory import EXECUTABLE_FORMULAS
from intent.taxonomy_schema import IntentTaxonomy, load_taxonomy

TAXONOMY_PATH = PROJECT_ROOT / "data" / "intent_taxonomy" / "intent_taxonomy.yaml"
ROUTING_PATH = PROJECT_ROOT / "data" / "intent_taxonomy" / "intent_routing_rules.yaml"

MW_PATTERN = re.compile(
    r"وزن\s*مولکول|molecular\s*weight|\bMW\b",
    re.IGNORECASE,
)
_CONVERSATIONAL_OFF_TOPIC = re.compile(
    r"(دلم(?:\s+\S+){0,4}\s*گرفته|حوصله\s*ندار|نمی\s*خوام|نمیخوام|بیخیال|"
    r"don't\s*want|feeling\s*down|i\s*am\s*sad|not\s*interested)",
    re.IGNORECASE,
)


@dataclass
class ClassificationResult:
    intent: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    requires_clarification: bool = False
    requires_session_context: bool = False
    expected_agents: list[str] = field(default_factory=list)
    numeric_safety_level: int = 0
    routing_trace: list[str] = field(default_factory=list)


class IntentClassifier:
    """Deterministic-first classifier; no LLM for routing decisions."""

    def __init__(self, taxonomy: IntentTaxonomy | None = None) -> None:
        self.taxonomy = taxonomy or load_taxonomy(TAXONOMY_PATH)
        self.intent_map = self.taxonomy.by_id()
        self.routing_rules = yaml.safe_load(ROUTING_PATH.read_text(encoding="utf-8"))

    def classify(
        self,
        query: str,
        *,
        slots: dict[str, Any] | None = None,
        session_ambiguous: bool = False,
        missing_slots: list[str] | None = None,
    ) -> ClassificationResult:
        q = query.strip()
        slots = dict(slots or {})
        trace: list[str] = []

        # Computed early so downstream rules (variable-explanation vs. actual
        # calculation) can disambiguate based on whether concrete numeric
        # inputs are present in the query text.
        _assign = r"(?:=|:|برابر(?:\s*با)?)\s*"
        calc_input_pattern = re.search(
            rf"(ahw_?\d*\s*{_assign}[\d\.]+|t_?\d*\s*{_assign}[\d\.]+|ahw\d+\s*[=:]?\s*[\d\.]+)",
            q,
            re.I,
        )

        if session_ambiguous or missing_slots:
            if slots.get("chemical_name") or slots.get("cas"):
                session_ambiguous = False
                missing_slots = [m for m in (missing_slots or []) if m not in ("chemical_name", "cas")]
            if session_ambiguous or missing_slots:
                intent = self._clarify_intent(missing_slots or [])
                trace.append(f"clarify:{intent}")
                meta = self.intent_map[intent]
                return ClassificationResult(
                    intent=intent,
                    confidence=0.95,
                    slots=slots,
                    requires_clarification=True,
                    expected_agents=["clarify"],
                    routing_trace=trace,
                )

        if slots.get("ambiguous_chemical"):
            trace.append("clarify:ambiguous_chemical")
            return ClassificationResult(
                intent="CLARIFY.MISSING_CHEMICAL",
                confidence=0.95,
                slots=slots,
                requires_clarification=True,
                expected_agents=["clarify"],
                routing_trace=trace,
            )

        # Conversational / emotional off-topic — not an OHSE task even if HSE words appear.
        if _CONVERSATIONAL_OFF_TOPIC.search(q):
            return self._result("GUARDRAIL.PROFESSIONAL_JUDGMENT", 0.88, slots, trace)

        # Guardrail patterns
        if re.search(r"(اخراج|تعلیق|بیمار|تشخیص بیماری|اخراج کنم)", q):
            return self._result("GUARDRAIL.PROFESSIONAL_JUDGMENT", 0.9, slots, trace)

        if re.search(r"\b(1[3-9]|[2-9]\d)\s*ساعت", q) and re.search(r"حد|مواجهه", q):
            return self._result("GUARDRAIL.UNSAFE_EXTRAPOLATION", 0.9, slots, trace)

        # Invalid formula calculation inputs: non-numeric value assigned to a
        # variable (e.g. "ahw1=abc") — malformed, not merely missing
        if re.search(r"(ahw|t)\d*\s*(?:=|:|برابر(?:\s*با)?)\s*[a-zA-Zآ-ی]+", q, re.I):
            return self._result("GUARDRAIL.INVALID_INPUT", 0.9, slots, trace)

        # Invalid formula calculation inputs (negative value / zero duration)
        for var, val in (slots.get("variables") or {}).items():
            if var.startswith("ahw") and val < 0:
                return self._result("GUARDRAIL.INVALID_INPUT", 0.92, slots, trace)
            if var.startswith("t") and val <= 0:
                return self._result("GUARDRAIL.INVALID_INPUT", 0.92, slots, trace)

        # Invalid / malformed CAS explicitly questioned
        if slots.get("cas") and re.search(r"معتبر|صحیح|درست است", q):
            if not is_valid_cas(slots["cas"]):
                return self._result("GUARDRAIL.INVALID_INPUT", 0.92, slots, trace)

        # Conflicting data claim
        if re.search(r"(دو\s*مقدار\s*مختلف|دو\s*عدد\s*متفاوت|متناقض|تناقض)", q):
            return self._result("GUARDRAIL.CONFLICTING_DATA", 0.88, slots, trace)

        # Unknown / placeholder chemical name → no data, not a normal lookup
        chem = slots.get("chemical_name") or ""
        placeholder_pattern = re.compile(r"\b(Unknown|NonExistent|Fake|Invalid|Nonexistent)\w*-?\d{2,}\b", re.IGNORECASE)
        if re.search(r"unknown", chem, re.IGNORECASE) or placeholder_pattern.search(q):
            return self._result("GUARDRAIL.NO_DATA", 0.9, slots, trace)

        # Hybrid (multi-intent markers) — numeric lookup + explanation in one query.
        # Broadened to match "X چقدر است و TWA یعنی چی؟" where the definition marker
        # is not immediately adjacent to "و" (e.g. separated by the OEL-type token).
        has_numeric_lookup_marker = re.search(r"(حد|چقدر|چنده)", q)
        has_definition_marker = re.search(r"(یعنی|چیست|چیه|تعریف|چه مفهوم|توضیح)", q)
        has_conjunction = re.search(r"\sو\s", q)
        if has_numeric_lookup_marker and has_definition_marker and has_conjunction:
            return self._result("HYBRID.LOOKUP_AND_EXPLAIN", 0.88, slots, trace)

        # Explicit TWA-vs-STEL (or Ceiling) comparison request
        if re.search(r"مقایسه", q) and re.search(r"\bTWA\b", q, re.I) and re.search(r"\b(STEL|Ceiling)\b", q, re.I):
            return self._result("HYBRID.LOOKUP_COMPARE_EXPLAIN", 0.86, slots, trace)

        if re.search(r"ppm|مواجهه|غلظت", q, re.I) and re.search(r"(بیشتر|مقایسه|نسبت|مجاز)", q):
            # STEL duration without explicit concentration → structured STEL, not hybrid
            if re.search(r"۱۵\s*دقیقه|15\s*min|STEL", q, re.I) and not re.search(r"\d+\s*ppm", q, re.I):
                if slots.get("chemical_name") or slots.get("cas"):
                    return self._result("STRUCTURED.OEL.STEL_LOOKUP", 0.88, slots, trace)
            return self._result("HYBRID.LOOKUP_COMPARE_EXPLAIN", 0.85, slots, trace)

        # HYBRID.FORMULA_AND_EXPLAIN: definition + usage/application ("کاربرد"),
        # distinct from a single-meaning interpretation question ("یعنی چی").
        if re.search(r"فرمول", q) and re.search(r"کاربرد", q) and re.search(r"\sو\s", q):
            return self._result("HYBRID.FORMULA_AND_EXPLAIN", 0.85, slots, trace)

        # Variable-specific explanation: "<letter/name> در formula_X یعنی چی؟" / "چه معنایی دارد؟"
        if re.search(r"\bformula_\d+_\d+\b", q, re.I) and re.search(r"\bدر\b", q) and re.search(
            r"(یعنی|چیه|چیست|معنایی\s+دارد|معنی)", q
        ):
            return self._result("FORMULA.VARIABLE.EXPLANATION", 0.86, slots, trace)

        # Variable/parameter explanation (asking WHAT the inputs are) — must come
        # before generic LOOKUP.BY_ID, and only applies when no concrete numeric
        # values are being supplied (that would be an actual calculation instead),
        # and it's a genuine question rather than a "calculate ... with default
        # inputs" command (the word "inputs"/"ورودی" there is filler, not a query).
        has_variable_marker = re.search(
            r"(متغیر|پارامتر|ورودی|چیزهایی\s+لازم|variables?|parameters?|inputs?)", q, re.I
        )
        is_calc_command_with_filler = re.search(r"(calculate|محاسبه\s*کن).*\bdefault\b", q, re.I)
        if (
            has_variable_marker
            and not calc_input_pattern
            and not slots.get("variables")
            and not is_calc_command_with_filler
            and (re.search(r"formula_\d+", q, re.I) or re.search(r"(فرمول|ahv|ارتعاش|محاسبه)", q))
        ):
            return self._result("FORMULA.VARIABLE.EXPLANATION", 0.82, slots, trace)

        if re.search(r"formula_\d+", q, re.I) and re.search(r"(چیست|چیه|چی\s)", q) and not re.search(r"(یعنی|مفهوم|توضیح|کاربرد)", q):
            return self._result("FORMULA.LOOKUP.BY_ID", 0.88, slots, trace)

        # Formula interpretation (result/formula meaning, not a specific variable)
        if re.search(r"(تفسیر|وضعیت|یعنی چ|معنی|اهمیت)", q) and re.search(r"(ahv|فرمول|نتیجه|ارتعاش|formula_)", q, re.I):
            return self._result("FORMULA.INTERPRETATION", 0.85, slots, trace)

        # Formula domain lookup — only when no specific formula number/id is given;
        # "شماره NN صفحه NNN" or an explicit formula_id identifies one exact formula (BY_ID).
        if re.search(r"(رابطه|فرمول).*(صفحه|ارتعاش|vibration|exposure|مواجهه)", q, re.I) and not re.search(
            r"formula_\d+|شماره\s*\d+", q, re.I
        ):
            return self._result("FORMULA.LOOKUP.BY_DOMAIN", 0.82, slots, trace)

        if re.search(r"شماره\s*\d+.*صفحه\s*\d+|صفحه\s*\d+.*شماره\s*\d+", q, re.I):
            return self._result("FORMULA.LOOKUP.BY_ID", 0.85, slots, trace)

        # Formula calculation — detect numeric inputs in query text (= : or Persian "برابر")
        formula_id_match = re.search(r"\bformula_\d+_\d+\b", q, re.I)
        # An attempted calculation carries some input-like content beyond the
        # bare verb + formula id (e.g. "با ahv=2", "with default inputs", "=").
        calc_attempt_with_content = bool(
            re.search(r"(با\s|with\s|=|default|ahv\s*=|ورودی)", q, re.I)
        )
        if calc_input_pattern or slots.get("variables") or re.search(
            r"ahv|محاسبه|حساب کن|mohasebe|compute|calculate", q, re.I
        ):
            # A calculation is requested for a specific formula that is not the
            # only executable one — unsupported, regardless of the exact variable
            # names used (registry-only formulas cannot be deterministically run).
            # Only when the query attempts to supply content (values/inputs);
            # a bare "calculate formula_X" defers to a clarification instead.
            if (
                formula_id_match
                and formula_id_match.group(0).lower() not in EXECUTABLE_FORMULAS
                and (calc_attempt_with_content or calc_input_pattern)
            ):
                return self._result("FORMULA.CALCULATION.UNSUPPORTED", 0.85, slots, trace)
            if slots.get("variables") or calc_input_pattern:
                return self._result("FORMULA.CALCULATION.VIBRATION_AHV", 0.9, slots, trace)
            if re.search(r"ahv|محاسبه|حساب", q, re.I):
                return self._result("CLARIFY.MISSING_EXPOSURE_VALUE", 0.85, slots, trace, clarify=True)
            if formula_id_match:
                return self._result("FORMULA.CALCULATION.UNSUPPORTED", 0.85, slots, trace)

        if re.search(r"فرمول|formula_", q, re.I):
            return self._result("FORMULA.LOOKUP.BY_ID", 0.82, slots, trace)

        # Semantic definitions (before structured numeric — «TWA یعنی چی؟» is definition not lookup)
        if re.search(r"BEI|زیستی|بیولوژ", q, re.I):
            return self._result("SEMANTIC.DEFINITION.BEI", 0.85, slots, trace)
        if re.search(r"(یعنی|چیست|چیه|تعریف)", q) and re.search(r"\bTWA\b", q, re.I):
            return self._result("SEMANTIC.DEFINITION.TWA", 0.9, slots, trace)
        if re.search(r"(یعنی|چیست|تعریف)", q) and re.search(r"\bSTEL\b", q, re.I):
            return self._result("SEMANTIC.DEFINITION.STEL", 0.9, slots, trace)
        if re.search(r"(یعنی|چیست|تعریف)", q) and re.search(r"Ceiling|سقف", q, re.I):
            return self._result("SEMANTIC.DEFINITION.CEILING", 0.88, slots, trace)
        if re.search(r"(یعنی|چیست|تعریف)", q) and re.search(r"OEL|حد مجاز مواجهه", q, re.I):
            return self._result("SEMANTIC.DEFINITION.OEL", 0.88, slots, trace)
        if re.search(r"LAeq|صدا.*(یعنی|تعریف)", q, re.I):
            return self._result("SEMANTIC.DEFINITION.NOISE", 0.85, slots, trace)
        if re.search(r"A\(8\)|ارتعاش.*(یعنی|تعریف)", q, re.I):
            return self._result("SEMANTIC.DEFINITION.VIBRATION", 0.85, slots, trace)

        # Structured OEL — route to BY_CAS only when the query itself identifies the
        # chemical via CAS number (raw CAS pattern or explicit "CAS" keyword used as
        # lookup key), not merely because a CAS happens to be present in slots
        # (e.g. inherited/known from a resolved chemical entity).
        query_has_cas_number = CAS_PATTERN.search(q) is not None
        explicit_cas_lookup = re.search(r"\bCAS\b", q, re.I) and not re.search(r"\bTWA\b|\bSTEL\b", q, re.I)

        # "CAS X چیست؟" (asking WHAT the CAS is, no CAS digits given) → look up the
        # chemical by name; the CAS is returned as part of the record, not used as
        # the search key.
        if (
            re.search(r"\bCAS\b", q, re.I)
            and re.search(r"چیست|چیه", q)
            and not query_has_cas_number
            and slots.get("chemical_name")
        ):
            return self._result("STRUCTURED.CHEMICAL.BY_NAME", 0.88, slots, trace)

        if slots.get("cas") and (query_has_cas_number or explicit_cas_lookup):
            return self._result("STRUCTURED.OEL.BY_CAS", 0.9, slots, trace)

        if MW_PATTERN.search(q):
            if slots.get("chemical_name") or slots.get("cas"):
                return self._result("STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT", 0.9, slots, trace)
            return self._result("CLARIFY.MISSING_CHEMICAL", 0.88, slots, trace, clarify=True)

        if re.search(r"منبع|صفحه|جدول", q) and slots.get("chemical_name"):
            return self._result("STRUCTURED.OEL.PROVENANCE", 0.85, slots, trace)

        if re.search(r"\bSTEL\b|کوتاه.?مدت|۱۵ دقیقه", q, re.I):
            if not slots.get("chemical_name") and not slots.get("cas"):
                return self._result("CLARIFY.MISSING_CHEMICAL", 0.9, slots, trace, clarify=True)
            return self._result("STRUCTURED.OEL.STEL_LOOKUP", 0.9, slots, trace)

        # Short follow-up with inherited chemical
        if slots.get("chemical_name") and len(q) < 20:
            if re.match(r"^STEL\s*\??$", q, re.I):
                return self._result("STRUCTURED.OEL.STEL_LOOKUP", 0.92, slots, trace)
            if re.match(r"^TWA\s*\??$", q, re.I):
                return self._result("STRUCTURED.OEL.TWA_LOOKUP", 0.92, slots, trace)
            if re.match(r"^CAS\s*\??$", q, re.I):
                return self._result("STRUCTURED.CHEMICAL.BY_NAME", 0.9, slots, trace)

        if re.search(r"Ceiling|سقف|\bC\b", q, re.I) and re.search(r"حد", q):
            if not slots.get("chemical_name") and not slots.get("cas"):
                return self._result("CLARIFY.MISSING_CHEMICAL", 0.9, slots, trace, clarify=True)
            return self._result("STRUCTURED.OEL.CEILING_LOOKUP", 0.88, slots, trace)

        # Explicit TWA keyword → TWA lookup regardless of other markers. Only
        # clarify when the query is short AND no chemical could be resolved —
        # a longer query (e.g. containing an adversarial "not X" distractor or a
        # numeric-prefixed name our regex misses) almost always still names one.
        if re.search(r"\bTWA\b|میانگین وزنی", q, re.I) or re.search(r"\btwa\b.*داره|\btwa\b.*دارد", q, re.I):
            if not slots.get("chemical_name") and not slots.get("cas") and len(q) < 25:
                return self._result("CLARIFY.MISSING_CHEMICAL", 0.92, slots, trace, clarify=True)
            return self._result("STRUCTURED.OEL.TWA_LOOKUP", 0.88, slots, trace)

        if re.search(r"حد|چقدر|چنده", q) and slots.get("chemical_name") and not MW_PATTERN.search(q):
            if re.search(r"همه|تمام|TWA و STEL", q):
                return self._result("STRUCTURED.OEL.ALL_LIMITS_LOOKUP", 0.88, slots, trace)
            # No explicit OEL type in the query itself: "X چقدره؟" asks for a value
            # without specifying which one → return all limits. A bare "حد X؟"
            # with no value marker at all is genuinely ambiguous about which
            # limit type is wanted → ask for clarification instead of guessing TWA.
            if not slots.get("oel_type"):
                if re.search(r"چقدر|چنده", q):
                    return self._result("STRUCTURED.OEL.ALL_LIMITS_LOOKUP", 0.86, slots, trace)
                return self._result("CLARIFY.MISSING_LIMIT_TYPE", 0.85, slots, trace, clarify=True)
            return self._result("STRUCTURED.OEL.TWA_LOOKUP", 0.88, slots, trace)

        if re.search(r"حد|چقدر|چنده", q):
            if not slots.get("chemical_name") and not slots.get("cas"):
                if len(q) < 25:
                    return self._result("CLARIFY.MISSING_CHEMICAL", 0.92, slots, trace, clarify=True)
                if re.search(r"برای \w+ چنده", q):
                    return self._result("CLARIFY.MISSING_LIMIT_TYPE", 0.9, slots, trace, clarify=True)

        # Semantic definitions (remaining)
        if re.search(r"توصیه|باید کرد|اقدام", q):
            return self._result("SEMANTIC.REGULATION.RECOMMENDATION", 0.8, slots, trace)
        if re.search(r"ممنوع|نباید|تشخیص", q):
            return self._result("SEMANTIC.REGULATION.PROHIBITION", 0.8, slots, trace)
        if re.search(r"کاربرد|حوزه|شهری", q):
            return self._result("SEMANTIC.REGULATION.SCOPE", 0.78, slots, trace)

        if re.search(r"(تعریف|چیست|چیه|یعنی چ|معنا)", q):
            return self._result("SEMANTIC.EXPLANATION.CONCEPT", 0.75, slots, trace)

        if re.search(r"(توضیح|چرا|چگونه|تفاوت)", q):
            return self._result("SEMANTIC.EXPLANATION.CONCEPT", 0.72, slots, trace)

        if re.search(r"خلاصه|بخش|فصل", q):
            return self._result("SEMANTIC.CONTEXT.SECTION", 0.75, slots, trace)

        # Short ambiguous
        if len(q) < 20 and re.search(r"حد|چقدر|چنده|مجاز", q):
            return self._result("CLARIFY.MISSING_CHEMICAL", 0.85, slots, trace, clarify=True)

        # Unrecognized intent is not professional judgment. Preserve explicit
        # GUARDRAIL.* rules above; otherwise allow document-grounded semantic retrieval.
        return self._result("SEMANTIC.EXPLANATION.CONCEPT", 0.5, slots, trace)

    def _clarify_intent(self, missing: list[str]) -> str:
        if "chemical_name" in missing or "cas" in missing:
            return "CLARIFY.MISSING_CHEMICAL"
        if "oel_type" in missing:
            return "CLARIFY.MISSING_LIMIT_TYPE"
        if "concentration" in missing:
            return "CLARIFY.MISSING_EXPOSURE_VALUE"
        if "duration" in missing:
            return "CLARIFY.MISSING_DURATION"
        return "CLARIFY.MISSING_AGENT"

    def _result(
        self,
        intent_id: str,
        confidence: float,
        slots: dict[str, Any],
        trace: list[str],
        *,
        clarify: bool = False,
    ) -> ClassificationResult:
        trace.append(f"intent:{intent_id}")
        meta = self.intent_map.get(intent_id)
        if not meta:
            return ClassificationResult(intent=intent_id, confidence=confidence, slots=slots, routing_trace=trace)
        agents = list(meta.hybrid_agents) if meta.agent == "hybrid" else [meta.agent]
        return ClassificationResult(
            intent=intent_id,
            confidence=confidence,
            slots=slots,
            requires_clarification=clarify or meta.requires_clarification,
            expected_agents=agents,
            numeric_safety_level=meta.numeric_safety_level,
            routing_trace=trace,
        )
