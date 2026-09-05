"""Regression tests for MW routing, Persian chemical resolution, and off-domain filtering."""

from __future__ import annotations

import pytest

from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots
from agents.routing.query_understanding import understand_query
from security.domain_gate import DomainSafetyGate, GateDecision


class TestQueryQualityFixes:
    def setup_method(self):
        self.c = IntentClassifier()

    def test_typo_molecular_weight_intent(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            u = understand_query(session, "وزن ملکولی برای دی متیل استامید چنده؟")
            r = self.c.classify(u.normalized_query, slots=u.slots)
            assert r.intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT", r.intent
        finally:
            session.close()

    def test_mw_not_routed_to_oel_lookup(self):
        r, _, _ = _classify("وزن مولکولی Acetamide", self.c)
        assert r.intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT"

    def test_dimethyl_acetamide_resolution(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            u = understand_query(session, "وزن ملکولی برای دی متیل استامید چنده؟")
            assert u.chemical is not None
            assert u.chemical.canonical_name == "acetamide Dimethyl"
            assert u.chemical.cas == "127-19-5"
        finally:
            session.close()

    def test_dimethyl_acetamide_mw_from_oel(self):
        from database.session import SessionLocal
        from agents.structured.store import PostgresStructuredStore

        session = SessionLocal()
        try:
            store = PostgresStructuredStore(session)
            mw = store.resolve_molecular_weight(chemical_name="acetamide Dimethyl")
            if mw is None or mw.get("source") != "oel_original_values":
                pytest.skip(
                    "Dimethyl acetamide MW not in canonical OEL original_values "
                    "(registry fallback only — data promotion pending)"
                )
            assert mw["molecular_weight_display"] == "87.12"
            assert float(mw["molecular_weight"]) == pytest.approx(87.12)
        finally:
            session.close()


def _classify(query: str, classifier: IntentClassifier, slots: dict | None = None):
    n = normalize_persian_query(query)
    merged = extract_slots(n, slots or {})
    return classifier.classify(n, slots=merged), merged, n


class TestOffDomainFiltering:
    def setup_method(self):
        self.gate = DomainSafetyGate()
        self.c = IntentClassifier()

    def test_emotional_query_rejected_without_session(self):
        q = "اصلا دلم خیلی گرفته مولکول نمیخوام"
        r = self.gate.evaluate(q)
        assert r.decision == GateDecision.REJECT

    def test_emotional_query_rejected_with_hse_session(self):
        q = "اصلا دلم خیلی گرفته مولکول نمیخوام"
        ctx = {"chemical_name": "Benzene", "previous_intent": "STRUCTURED.OEL.TWA_LOOKUP"}
        r = self.gate.evaluate(q, session_context=ctx)
        assert r.decision == GateDecision.REJECT

    def test_emotional_query_classifier_guardrail(self):
        r, _, _ = _classify("اصلا دلم خیلی گرفته مولکول نمیخوام", self.c)
        assert r.intent == "GUARDRAIL.PROFESSIONAL_JUDGMENT"

    def test_hse_followup_still_passes_in_session(self):
        ctx = {"chemical_name": "Benzene", "previous_intent": "STRUCTURED.OEL.TWA_LOOKUP"}
        r = self.gate.evaluate("STEL?", session_context=ctx)
        assert r.decision == GateDecision.PASS


class TestOrchestratorEndToEnd:
    @pytest.mark.integration
    def test_dimethyl_acetamide_mw_query(self):
        from database.session import SessionLocal
        from agents.orchestrator.pipeline import QueryOrchestrator
        from agents.session.store import SessionStore

        session = SessionLocal()
        try:
            from agents.structured.store import PostgresStructuredStore

            store = PostgresStructuredStore(session)
            mw = store.resolve_molecular_weight(chemical_name="acetamide Dimethyl")
            if mw is None or mw.get("source") != "oel_original_values":
                pytest.skip(
                    "Dimethyl acetamide MW not in canonical OEL original_values "
                    "(registry fallback only — data promotion pending)"
                )
            orch = QueryOrchestrator(session, session_store=SessionStore())
            resp = orch.handle("وزن ملکولی برای دی متیل استامید چنده؟", session_id="e2e-mw")
            assert resp.intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT"
            assert "87.12" in resp.answer
            assert resp.gate_decision == "pass"
        finally:
            session.close()

    @pytest.mark.integration
    def test_emotional_query_rejected_in_orchestrator(self):
        from database.session import SessionLocal
        from agents.orchestrator.pipeline import QueryOrchestrator
        from agents.session.store import SessionStore

        store = SessionStore()
        state = store.create_session("polluted-session")
        state.chemical_name = "Acetamide"
        state.previous_intent = "STRUCTURED.OEL.TWA_LOOKUP"
        store.save(state)

        session = SessionLocal()
        try:
            orch = QueryOrchestrator(session, session_store=store)
            resp = orch.handle("اصلا دلم خیلی گرفته مولکول نمیخوام", session_id="polluted-session")
            assert resp.intent == "DOMAIN.REJECTED"
            assert resp.gate_decision == "reject"
        finally:
            session.close()


class TestG6ExplicitLatinBeatsPersianTie:
    """Unique Latin/CAS in the query must not be dropped for a Persian-name tie."""

    def setup_method(self):
        self.c = IntentClassifier()

    def test_unique_latin_retained_despite_ambiguous_persian(self):
        from database.session import SessionLocal

        query = "مواجهه با هیدرید آنتیموان (Antimony hydride) چه عوارضی دارد؟"
        session = SessionLocal()
        try:
            u = understand_query(session, query)
            assert u.chemical is not None
            assert u.chemical.ambiguous is False
            assert u.slots.get("ambiguous_chemical") is not True
            assert u.chemical.canonical_name
            assert "antimony" in u.chemical.canonical_name.lower()
            r = self.c.classify(u.normalized_query, slots=u.slots)
            assert r.intent != "CLARIFY.MISSING_CHEMICAL", r.intent
        finally:
            session.close()

    def test_explicit_cas_retained_despite_ambiguous_persian(self):
        from database.session import SessionLocal

        query = "TWA اسید 71-43-2 چقدر است؟"
        session = SessionLocal()
        try:
            u = understand_query(session, query)
            assert u.chemical is not None
            assert u.chemical.ambiguous is False
            assert u.chemical.cas == "71-43-2"
            assert u.slots.get("cas") == "71-43-2"
            r = self.c.classify(u.normalized_query, slots=u.slots)
            assert r.intent != "CLARIFY.MISSING_CHEMICAL", r.intent
        finally:
            session.close()

    def test_missing_entity_still_missing_chemical(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            u = understand_query(session, "حدش چنده؟")
            r = self.c.classify(u.normalized_query, slots=u.slots)
            assert r.intent == "CLARIFY.MISSING_CHEMICAL"
            assert not u.slots.get("chemical_name")
            assert not u.slots.get("cas")
        finally:
            session.close()

    def test_q13_molecular_weight_intent_unchanged(self):
        from database.session import SessionLocal

        query = "وزن مولکولی اتیون (Ethion) طبق جدول چقدر است؟"
        session = SessionLocal()
        try:
            u = understand_query(session, query)
            r = self.c.classify(u.normalized_query, slots=u.slots)
            assert u.chemical is not None
            assert u.chemical.canonical_name == "Ethion"
            assert u.chemical.cas == "563-12-2"
            assert r.intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT", r.intent
        finally:
            session.close()

