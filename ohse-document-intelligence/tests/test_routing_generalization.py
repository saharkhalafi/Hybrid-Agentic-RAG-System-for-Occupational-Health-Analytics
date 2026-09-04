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
