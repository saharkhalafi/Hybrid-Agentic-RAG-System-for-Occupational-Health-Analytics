"""Focused routing tests: unseen wording must reach semantic, not reject/PJ."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.router import QueryRouter
from agents.routing.slots import extract_slots
from security.domain_gate import DomainSafetyGate, GateDecision

# Regression examples (not keyword special-cases).
Q01 = "نماد پوست برای چه نوع موادی به کار می‌رود؟"
Q02 = "چرا حساسیت‌زایی حتی در غلظت‌های کمتر از OEL می‌تواند خطرناک باشد؟"
Q03 = "آیا نماد پوست برای موادی که فقط باعث تحریک پوستی می‌شوند استفاده می‌شود؟"
# Unseen wording with no current template/vocab hits.
UNSEEN = "نشان کنار اسم ترکیب را چطور باید تفسیر کرد؟"
OFF_DOMAIN = "نتیجه بازی فوتبال دیشب چی شد؟"
HR_JUDGMENT = "آیا باید این کارگر را از کار اخراج کنم؟"


def _classify(query: str, classifier: IntentClassifier, slots: dict | None = None):
    n = normalize_persian_query(query)
    merged = extract_slots(n, slots or {})
    return classifier.classify(n, slots=merged)


def _router() -> QueryRouter:
    return QueryRouter(MagicMock(), MagicMock(), MagicMock(), MagicMock())


class TestUnseenWordingReachesSemantic:
    def setup_method(self):
        self.gate = DomainSafetyGate()
        self.classifier = IntentClassifier()
        self.router = _router()

    def _assert_semantic_path(self, query: str) -> None:
        gate = self.gate.evaluate(query)
        assert gate.decision == GateDecision.PASS, (query, gate.decision, gate.reasons)
        result = _classify(query, self.classifier)
        assert result.intent == "SEMANTIC.EXPLANATION.CONCEPT", (query, result.intent)
        assert result.intent != "GUARDRAIL.PROFESSIONAL_JUDGMENT"
        planned = self.router.plan(result)
        assert "semantic" in planned
        assert "guardrail" not in planned

    def test_q01_unseen_wording_reaches_semantic(self):
        self._assert_semantic_path(Q01)

    def test_q03_unseen_wording_reaches_semantic(self):
        self._assert_semantic_path(Q03)

    def test_unseen_wording_without_known_templates_reaches_semantic(self):
        self._assert_semantic_path(UNSEEN)


class TestPreservedRouting:
    def setup_method(self):
        self.gate = DomainSafetyGate()
        self.classifier = IntentClassifier()
        self.router = _router()

    def test_q02_explanation_unchanged(self):
        gate = self.gate.evaluate(Q02)
        assert gate.decision == GateDecision.PASS
        result = _classify(Q02, self.classifier)
        assert result.intent == "SEMANTIC.EXPLANATION.CONCEPT"
        assert "semantic" in self.router.plan(result)

    def test_clearly_unrelated_still_rejected(self):
        gate = self.gate.evaluate(OFF_DOMAIN)
        assert gate.decision == GateDecision.REJECT
        assert "off_domain" in gate.reasons

    def test_hr_judgment_still_guardrail(self):
        result = _classify(HR_JUDGMENT, self.classifier)
        assert result.intent == "GUARDRAIL.PROFESSIONAL_JUDGMENT"
        planned = self.router.plan(result)
        executed = self.router.execute(result, HR_JUDGMENT, {})
        assert planned == ["guardrail"] or executed.get("guardrail")
        assert "semantic" not in executed

    def test_structured_twa_lookup_unchanged(self):
        result = _classify("TWA بنزن چقدره؟", self.classifier, slots={"chemical_name": "Benzene"})
        assert result.intent == "STRUCTURED.OEL.TWA_LOOKUP"
        assert self.router.plan(result) == ["structured"]

    def test_formula_lookup_unchanged(self):
        result = _classify("formula_240_01 چیست؟", self.classifier)
        assert result.intent.startswith("FORMULA.")
        assert "formula" in self.router.plan(result)

    def test_formula_identity_question_is_document_retrieval_not_clarify(self):
        q = (
            "فرمول محاسبهٔ توده ذرات قابل‌تنفس (IPM) بر حسب قطر آئرودینامیکی "
            "چیست و در چه بازه‌ای معتبر است؟"
        )
        result = _classify(q, self.classifier)
        assert result.intent != "CLARIFY.MISSING_EXPOSURE_VALUE"
        assert not result.requires_clarification
        assert result.intent == "SEMANTIC.EXPLANATION.CONCEPT"
        planned = self.router.plan(result)
        assert "semantic" in planned
        assert "clarify" not in planned
        assert "formula" not in planned

    def test_missing_calc_inputs_still_clarify(self):
        result = _classify("ahv را محاسبه کن", self.classifier)
        assert result.intent == "CLARIFY.MISSING_EXPOSURE_VALUE"
        assert result.requires_clarification
        executed = self.router.execute(result, "ahv را محاسبه کن", {})
        assert executed.get("guardrail")
        assert "formula" not in executed
        assert "semantic" not in executed

    def test_formula_lookup_with_explicit_id_unchanged(self):
        result = _classify("فرمول formula_240_01 چیست؟", self.classifier)
        assert result.intent == "FORMULA.LOOKUP.BY_ID"
        assert self.router.plan(result) == ["formula"]

    def test_numeric_ahv_calculation_unchanged(self):
        q = "با ahw1=1.5,t1=3,ahw2=2.5,t2=5 ahv را محاسبه کن"
        result = _classify(
            q,
            self.classifier,
            slots={"variables": {"ahw_1": 1.5, "t_1": 3.0, "ahw_2": 2.5, "t_2": 5.0}},
        )
        assert result.intent == "FORMULA.CALCULATION.VIBRATION_AHV"
        assert self.router.plan(result) == ["formula"]


class TestHybridCompareNotOelNounPhrase:
    def setup_method(self):
        self.classifier = IntentClassifier()

    def test_oel_noun_phrase_is_not_compare(self):
        result = _classify("طبق جدول، مبنای تعیین حد مجاز مواجهه شغلی برای EPN چیست؟", self.classifier)
        assert result.intent != "HYBRID.LOOKUP_COMPARE_EXPLAIN", result.intent

    def test_over_limit_exposure_still_compare(self):
        result = _classify("مواجهه 2 ppm بنزن بیشتر از حد مجاز است؟", self.classifier)
        assert result.intent == "HYBRID.LOOKUP_COMPARE_EXPLAIN"

    def test_twa_stel_comparison_still_compare(self):
        result = _classify("مقایسه TWA و STEL Ammonia", self.classifier)
        assert result.intent == "HYBRID.LOOKUP_COMPARE_EXPLAIN"


class TestProfessionalJudgmentTokenBoundary:
    def setup_method(self):
        self.classifier = IntentClassifier()

    def test_occupational_disease_question_is_not_professional_judgment(self):
        result = _classify(
            "مواجهه با Nickel subsulfide چه بیماری‌ای ایجاد می‌کند؟",
            self.classifier,
        )
        assert result.intent != "GUARDRAIL.PROFESSIONAL_JUDGMENT", result.intent

    def test_workplace_dismissal_still_professional_judgment(self):
        result = _classify("آیا باید این کارگر را از کار اخراج کنم؟", self.classifier)
        assert result.intent == "GUARDRAIL.PROFESSIONAL_JUDGMENT"


class TestHadTokenAndNonChemicalLatinSlots:
    def setup_method(self):
        self.classifier = IntentClassifier()

    def test_maximum_word_is_not_missing_limit_type(self):
        q = "شماره ثبت چکیده شیمیایی (CAS Number) از چند بخش تشکیل شده و حداکثر چند رقم دارد؟"
        result = _classify(q, self.classifier)
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent
        name = (extract_slots(normalize_persian_query(q)).get("chemical_name") or "").lower()
        assert name not in {"number", "cas number"}

    def test_constraint_word_is_not_missing_limit_type(self):
        result = _classify(
            "معیار تراز فشار صوت قله (Peak-SPL) برای صداهای پیوسته چه محدودیتی دارد؟",
            self.classifier,
        )
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent

    def test_physiological_limit_phrase_is_not_missing_limit_type(self):
        result = _classify(
            "NIOSH چه فشار جزئی اکسیژن آلوئولی را حد فیزیولوژیک تعیین کرده است؟",
            self.classifier,
        )
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent

    def test_peak_spl_label_is_not_missing_limit_type(self):
        result = _classify("Peak-SPL چه محدودیتی دارد؟", self.classifier)
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent
        slots = extract_slots("Peak-SPL چه محدودیتی دارد؟")
        assert "chemical_name" not in slots or not slots.get("chemical_name")

    def test_bare_limit_query_still_asks_limit_type(self):
        n = normalize_persian_query("حد Anthracite؟")
        slots = extract_slots(n)
        slots["chemical_name"] = "Anthracite"
        result = self.classifier.classify(n, slots=slots)
        assert result.intent == "CLARIFY.MISSING_LIMIT_TYPE"

    def test_q13_molecular_weight_routing_unchanged(self):
        result = _classify("وزن مولکولی Acetamide", self.classifier)
        assert result.intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT"


class TestLimitTypeConceptNotClarify:
    def setup_method(self):
        self.classifier = IntentClassifier()
        self.router = _router()

    def test_oel_column_meaning_without_chemical_is_semantic(self):
        q = "خالی بودن یکی از حدود TWA یا STEL برای یک ماده به چه معناست؟"
        result = _classify(q, self.classifier)
        assert result.intent != "CLARIFY.MISSING_CHEMICAL", result.intent
        assert not result.requires_clarification
        assert result.intent.startswith("SEMANTIC.")
        assert "semantic" in self.router.plan(result)
        assert "clarify" not in self.router.plan(result)

    def test_short_stel_without_entity_still_missing_chemical(self):
        result = _classify("STEL؟", self.classifier)
        assert result.intent == "CLARIFY.MISSING_CHEMICAL"
        assert result.requires_clarification

    def test_stel_lookup_with_chemical_unchanged(self):
        result = _classify("STEL بنزن چنده؟", self.classifier, slots={"chemical_name": "Benzene"})
        assert result.intent == "STRUCTURED.OEL.STEL_LOOKUP"

    def test_twa_lookup_with_chemical_unchanged(self):
        result = _classify("TWA Ammonia چنده؟", self.classifier, slots={"chemical_name": "Ammonia"})
        assert result.intent == "STRUCTURED.OEL.TWA_LOOKUP"

    def test_oel_scope_question_not_missing_limit_type(self):
        n = normalize_persian_query(
            "حد مجاز مواجهه شغلی این گروه از ذرات شامل چه موادی می‌شود؟"
        )
        slots = extract_slots(n)
        slots["chemical_name"] = "ResolvedLabel"
        result = self.classifier.classify(n, slots=slots)
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent
        assert not result.requires_clarification
        assert result.intent.startswith("SEMANTIC.")

    def test_determined_limit_concept_not_missing_limit_type(self):
        n = normalize_persian_query(
            "این مرجع چه مقداری را حد فیزیولوژیک تعیین کرده است؟"
        )
        slots = extract_slots(n)
        slots["chemical_name"] = "ResolvedAgency"
        result = self.classifier.classify(n, slots=slots)
        assert result.intent != "CLARIFY.MISSING_LIMIT_TYPE", result.intent
        assert not result.requires_clarification
        assert result.intent.startswith("SEMANTIC.")

    def test_explicit_twa_value_request_still_structured(self):
        result = _classify(
            "TWA Anthracite چقدر است؟",
            self.classifier,
            slots={"chemical_name": "Anthracite"},
        )
        assert result.intent == "STRUCTURED.OEL.TWA_LOOKUP"

