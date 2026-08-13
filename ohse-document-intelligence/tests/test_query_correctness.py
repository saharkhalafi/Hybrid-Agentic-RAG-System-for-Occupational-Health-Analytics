"""Regression tests for query correctness — session, entity resolution, lookup."""

from __future__ import annotations

import pytest

from agents.routing.chemical_registry_cache import reset_chemical_registry_cache
from agents.routing.classifier import IntentClassifier
from agents.routing.entity_signals import query_has_explicit_entity_signal
from agents.routing.normalizer import normalize_persian_query
from agents.routing.query_understanding import understand_query
from agents.routing.slots import extract_slots
from agents.session.context import SessionContextManager
from agents.session.store import SessionStore


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_chemical_registry_cache()
    yield
    reset_chemical_registry_cache()


class TestEntitySignals:
    def test_persian_chemical_is_explicit_entity(self):
        assert query_has_explicit_entity_signal("twe برای استونیتریل چقدره")
        assert query_has_explicit_entity_signal("حد مجاز فیبرهای سیلیکات آلومینیوم چقدره")

    def test_bare_limit_type_is_not_explicit_entity(self):
        assert not query_has_explicit_entity_signal("STEL?")
        assert not query_has_explicit_entity_signal("TWA?")


class TestLimitTypeTypoNormalization:
    def test_twe_normalized_to_twa(self):
        assert "TWA" in normalize_persian_query("twe برای استونیتریل چقدره")


class TestSlotInheritance:
    def test_extract_slots_does_not_inherit_chemical_identity(self):
        inherited = {
            "chemical_name": "Acetonitrile",
            "cas": "75-05-8",
            "oel_type": "STEL",
        }
        slots = extract_slots("حدش چنده؟", inherited)
        assert "chemical_name" not in slots
        assert "cas" not in slots
        assert slots.get("oel_type") == "STEL"


class TestSessionContamination:
    def test_new_persian_chemical_not_treated_as_followup(self):
        store = SessionStore()
        state = store.create_session("sess-1")
        state.chemical_name = "Acetonitrile"
        state.cas = "75-05-8"
        state.turn_id = 1
        store.save(state)

        ctx_mgr = SessionContextManager(store)
        _, ctx = ctx_mgr.begin_turn("sess-1", "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره")
        assert "followup_detected" not in ctx.context_trace
        assert "inherited_chemical" not in " ".join(ctx.context_trace)
        assert ctx.inherited_slots.get("chemical_name") is None

    def test_genuine_followup_still_inherits(self):
        store = SessionStore()
        state = store.create_session("sess-2")
        state.chemical_name = "Benzene"
        state.turn_id = 1
        store.save(state)

        ctx_mgr = SessionContextManager(store)
        _, ctx = ctx_mgr.begin_turn("sess-2", "STEL?")
        assert ctx.inherited_slots.get("chemical_name") == "Benzene"


@pytest.mark.integration
class TestQueryCorrectnessIntegration:
    def test_twe_acetonitrile_routes_to_twa_not_clarify(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            u = understand_query(session, "TWA برای استونیتریل چقدره", raw_query="twe برای استونیتریل چقدره")
            assert u.chemical is not None
            assert u.chemical.cas == "75-05-8"
            assert u.slots.get("oel_type") == "TWA"
            c = IntentClassifier()
            r = c.classify(u.normalized_query, slots=u.slots)
            assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"
        finally:
            session.close()

    def test_aluminosilicate_resolves_not_acetonitrile(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            u = understand_query(
                session,
                "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره",
            )
            assert u.chemical is not None
            assert u.chemical.ambiguous is False
            assert u.chemical.cas == "142844-00-6"
        finally:
            session.close()

    def test_polluted_session_no_wrong_chemical(self):
        from database.session import SessionLocal
        from agents.orchestrator.pipeline import QueryOrchestrator

        store = SessionStore()
        state = store.create_session("regression-polluted")
        state.chemical_name = "Acetonitrile"
        state.cas = "75-05-8"
        state.turn_id = 1
        store.save(state)

        session = SessionLocal()
        try:
            orch = QueryOrchestrator(session, session_store=store)
            orch.handle("STEL acetonitrile 75-05-8", session_id="regression-polluted")
            resp = orch.handle(
                "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره",
                session_id="regression-polluted",
            )
            assert "Acetonitrile" not in (resp.answer or "")
            trace_slots = resp.metadata.get("trace", {}).get("slots", {})
            assert trace_slots.get("cas") == "142844-00-6"
        finally:
            session.close()

    def test_unrelated_query_fresh_session_id(self):
        from database.session import SessionLocal
        from agents.orchestrator.pipeline import QueryOrchestrator

        session = SessionLocal()
        try:
            orch = QueryOrchestrator(session, session_store=SessionStore())
            r = orch.handle("twe برای استونیتریل چقدره", session_id="fresh-a")
            assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"
            assert r.metadata["trace"]["slots"]["cas"] == "75-05-8"
        finally:
            session.close()

    def test_amino_butanol_twa_resolves_from_book(self):
        from database.session import SessionLocal
        from agents.orchestrator.pipeline import QueryOrchestrator

        session = SessionLocal()
        try:
            orch = QueryOrchestrator(session, session_store=SessionStore())
            resp = orch.handle("آمینو بوتانول twa چی میشه", session_id="amino-butanol-test")
            slots = resp.metadata.get("trace", {}).get("slots", {})
            assert slots.get("cas") == "96-20-8", slots
            assert resp.intent == "STRUCTURED.OEL.TWA_LOOKUP", resp.intent
            assert "1.0" in (resp.answer or ""), resp.answer
            assert slots.get("resolution_source") == "explicit_query"
        finally:
            session.close()
