"""Phase C.4 — Retrieval & Agent Quality Hardening regression tests.

Covers the routing/classifier fixes that produced the largest measurable
accuracy gains: guardrail detection, the BY_CAS over-triggering fix, hybrid
lookup+explain broadening, formula routing disambiguation, and the
ALL_LIMITS / CLARIFY.MISSING_LIMIT_TYPE split.
"""

from __future__ import annotations

from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots, is_valid_cas


def _classify(query: str, classifier: IntentClassifier):
    n = normalize_persian_query(query)
    slots = extract_slots(n)
    return classifier.classify(n, slots=slots)


class TestCasValidation:
    def test_valid_cas_checksum(self):
        assert is_valid_cas("50-00-0")  # formaldehyde

    def test_invalid_cas_checksum(self):
        assert not is_valid_cas("12-34-5")

    def test_malformed_cas_format(self):
        assert not is_valid_cas("not-a-cas")


class TestGuardrailRouting:
    def setup_method(self):
        self.c = IntentClassifier()

    def test_invalid_formula_input_negative(self):
        r = _classify("ahv را با ahw1=-1 و t1=0 محاسبه کن (formula_240_01)", self.c)
        assert r.intent == "GUARDRAIL.INVALID_INPUT"

    def test_invalid_formula_input_non_numeric(self):
        r = _classify("محاسبه ahv: ahw1=abc, t1=xyz (formula_240_01)", self.c)
        assert r.intent == "GUARDRAIL.INVALID_INPUT"

    def test_unknown_chemical_placeholder(self):
        r = _classify("حد مجاز ماده UnknownChem-9999 چقدر است؟", self.c)
        assert r.intent == "GUARDRAIL.NO_DATA"

    def test_nonexistent_chemical_placeholder(self):
        r = _classify("حد مجاز ماده NonExistent-1000 چقدر است؟", self.c)
        assert r.intent == "GUARDRAIL.NO_DATA"

    def test_professional_judgment(self):
        r = _classify("آیا باید این کارگر را از کار اخراج کنم؟", self.c)
        assert r.intent == "GUARDRAIL.PROFESSIONAL_JUDGMENT"

    def test_unsafe_extrapolation(self):
        r = _classify("برای ۳۰ ساعت مواجهه روزانه حد مجاز را بگو", self.c)
        assert r.intent == "GUARDRAIL.UNSAFE_EXTRAPOLATION"

    def test_invalid_cas_explicit_question(self):
        r = _classify("CAS 12-34-5 معتبر است؟", self.c)
        assert r.intent == "GUARDRAIL.INVALID_INPUT"

    def test_conflicting_data(self):
        r = _classify("چرا دو مقدار مختلف برای TWA بنزن دارید؟", self.c)
        assert r.intent == "GUARDRAIL.CONFLICTING_DATA"


class TestByCasOvertrigger:
    """Presence of a CAS in slots (e.g. inherited from session) must not force
    BY_CAS routing unless the query itself identifies the chemical via CAS."""

    def setup_method(self):
        self.c = IntentClassifier()

    def test_twa_query_with_inherited_cas_stays_twa_lookup(self):
        n = normalize_persian_query("TWA Ammonia چنده؟")
        slots = extract_slots(n)
        slots["cas"] = "7664-41-7"  # inherited from session, not in query text
        r = self.c.classify(n, slots=slots)
        assert r.intent != "STRUCTURED.OEL.BY_CAS"

    def test_explicit_cas_number_in_query_routes_by_cas(self):
        n = normalize_persian_query("حد مجاز CAS 71-43-2 چقدر است؟")
        slots = extract_slots(n)
        r = self.c.classify(n, slots=slots)
        assert r.intent == "STRUCTURED.OEL.BY_CAS"

    def test_cas_question_routes_to_chemical_by_name(self):
        n = normalize_persian_query("CAS Benzene چیست؟")
        slots = extract_slots(n)
        r = self.c.classify(n, slots=slots)
        assert r.intent == "STRUCTURED.CHEMICAL.BY_NAME"


class TestHybridBroadening:
    def setup_method(self):
        self.c = IntentClassifier()

    def test_lookup_and_explain_separated_by_oel_type(self):
        r = _classify("حد TWA Acetone چقدر است و TWA یعنی چی؟", self.c)
        assert r.intent == "HYBRID.LOOKUP_AND_EXPLAIN"

    def test_formula_and_explain_with_kaarbord(self):
        r = _classify("فرمول formula_240_01 چیست و چه کاربردی دارد؟", self.c)
        assert r.intent == "HYBRID.FORMULA_AND_EXPLAIN"

    def test_formula_interpretation_not_hybrid(self):
        r = _classify("فرمول formula_240_01 یعنی چی و چرا مهم است؟", self.c)
        assert r.intent == "FORMULA.INTERPRETATION"

    def test_twa_stel_comparison(self):
        r = _classify("مقایسه TWA و STEL Ammonia", self.c)
        assert r.intent == "HYBRID.LOOKUP_COMPARE_EXPLAIN"


class TestFormulaRoutingDisambiguation:
    def setup_method(self):
        self.c = IntentClassifier()

    def test_variable_explanation_english(self):
        r = _classify("variables of formula_240_01", self.c)
        assert r.intent == "FORMULA.VARIABLE.EXPLANATION"

    def test_variable_explanation_persian_parameters(self):
        r = _classify("پارامترهای formula_240_01 چیه؟", self.c)
        assert r.intent == "FORMULA.VARIABLE.EXPLANATION"

    def test_calculation_with_numeric_inputs_not_explanation(self):
        r = _classify(
            "با پارامترهای ahw1=1.5, t1=3.0, ahw2=2.5, t2=5.0 ahv چنده؟", self.c
        )
        assert r.intent == "FORMULA.CALCULATION.VIBRATION_AHV"

    def test_unsupported_formula_calc_with_content(self):
        r = _classify("calculate formula_240_02 with default inputs", self.c)
        assert r.intent == "FORMULA.CALCULATION.UNSUPPORTED"

    def test_bare_calculate_unsupported_formula_asks_clarify(self):
        r = _classify("محاسبه formula_240_02", self.c)
        assert r.intent == "CLARIFY.MISSING_EXPOSURE_VALUE"

    def test_variable_meaning_question(self):
        r = _classify("ahw در formula_240_01 چه معنایی دارد؟", self.c)
        assert r.intent == "FORMULA.VARIABLE.EXPLANATION"

    def test_persian_baraabar_assignment_parsed(self):
        n = normalize_persian_query(
            "اگر ahw1 برابر 1.5 و t1 برابر 3.0 باشد ahv چقدر می‌شود؟"
        )
        slots = extract_slots(n)
        assert slots.get("variables") == {"ahw_1": 1.5, "t_1": 3.0}


class TestLimitTypeDisambiguation:
    def setup_method(self):
        self.c = IntentClassifier()

    def test_bare_limit_query_asks_clarify_limit_type(self):
        n = normalize_persian_query("حد Anthracite؟")
        slots = extract_slots(n)
        slots["chemical_name"] = "Anthracite"
        r = self.c.classify(n, slots=slots)
        assert r.intent == "CLARIFY.MISSING_LIMIT_TYPE"

    def test_value_question_without_type_returns_all_limits(self):
        n = normalize_persian_query("حد مجاز Copper چقدره؟")
        slots = extract_slots(n)
        slots["chemical_name"] = "Copper"
        r = self.c.classify(n, slots=slots)
        assert r.intent == "STRUCTURED.OEL.ALL_LIMITS_LOOKUP"

    def test_explicit_twa_keyword_still_routes_twa(self):
        n = normalize_persian_query("TWA Ammonia چنده؟")
        slots = extract_slots(n)
        slots["chemical_name"] = "Ammonia"
        r = self.c.classify(n, slots=slots)
        assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"
