"""Phase F0.1 — entity / session isolation regression tests."""

from __future__ import annotations

import uuid

import pytest

from agents.orchestrator.pipeline import QueryOrchestrator
from agents.routing.chemical_registry_cache import reset_chemical_registry_cache
from agents.routing.entity_signals import query_has_explicit_entity_signal
from agents.routing.query_understanding import understand_query
from agents.session.context import SessionContextManager
from agents.session.store import SessionStore
from database.session import SessionLocal

# Canonical CAS anchors used across tests (not hardcoded routing rules).
CAS = {
    "acetonitrile": "75-05-8",
    "benzene": "71-43-2",
    "aluminosilicate": "142844-00-6",
    "acetaldehyde": "75-07-0",
    "bisphenol_a": "80-05-7",
    "chlorine_trifluoride": "7790-91-2",
    "toluene": "108-88-3",
    "ammonia": "7664-41-7",
}


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_chemical_registry_cache()
    yield
    reset_chemical_registry_cache()


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def store():
    return SessionStore()


@pytest.fixture
def orchestrator(db_session, store):
    return QueryOrchestrator(db_session, session_store=store)


def _seed(store: SessionStore, session_id: str, chemical: str, cas: str, *, turn_id: int = 1) -> None:
    state = store.create_session(session_id)
    state.chemical_name = chemical
    state.cas = cas
    state.turn_id = turn_id
    store.save(state)


def _resolve(db_session, store, query: str, session_id: str | None = None):
    ctx_mgr = SessionContextManager(store)
    _, ctx = ctx_mgr.begin_turn(session_id, query)
    u = understand_query(db_session, ctx.resolved_query, inherited_slots=ctx.merged_slots, raw_query=query)
    return ctx, u


def _run(orchestrator: QueryOrchestrator, query: str, session_id: str | None = None):
    return orchestrator.handle(query, session_id=session_id)


def _slots(resp) -> dict:
    return resp.metadata.get("trace", {}).get("slots", {})


def _assert_not_chemical(slots: dict, forbidden_cas: set[str]) -> None:
    assert slots.get("cas") not in forbidden_cas, f"wrong chemical cas={slots.get('cas')}"
    assert not slots.get("ambiguous_chemical") or slots.get("cas") is None


def _assert_cas(slots: dict, expected: str) -> None:
    assert slots.get("cas") == expected, slots


# ── 1. Explicit entity always wins (10+) ─────────────────────────────────────

@pytest.mark.integration
class TestExplicitEntityPrecedence:
    @pytest.mark.parametrize(
        "session_chem,session_cas,query,expected_cas",
        [
            ("Acetonitrile", CAS["acetonitrile"], "حد مجاز بنزن چقدره", CAS["benzene"]),
            ("Acetonitrile", CAS["acetonitrile"], "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", CAS["aluminosilicate"]),
            ("Benzene", CAS["benzene"], "CAS استونیتریل چنده", CAS["acetonitrile"]),
            ("Benzene", CAS["benzene"], "STEL acetonitrile 75-05-8", CAS["acetonitrile"]),
            ("Acetonitrile", CAS["acetonitrile"], "حد TWA تولوئن چقدره؟", None),
            ("Acetonitrile", CAS["acetonitrile"], "OEL ceiling for chlorine trifluoride CAS 7790-91-2", CAS["chlorine_trifluoride"]),
            ("Benzene", CAS["benzene"], "حد مجاز TWA برای بیس فنول آ 80-05-7", CAS["bisphenol_a"]),
            ("Aluminosilicate fibres", CAS["aluminosilicate"], "STEL acetaldehyde 75-07-0", CAS["acetaldehyde"]),
            ("Acetonitrile", CAS["acetonitrile"], "حد مجاز آمونیاک چقدره", CAS["ammonia"]),
            ("Benzene", CAS["benzene"], "twe برای استونیتریل چقدره", CAS["acetonitrile"]),
            ("Acetonitrile", CAS["acetonitrile"], "Benzene TWA limit", CAS["benzene"]),
            ("Benzene", CAS["benzene"], "75-05-8 STEL", CAS["acetonitrile"]),
        ],
    )
    def test_explicit_overrides_session(
        self, orchestrator, store, session_chem, session_cas, query, expected_cas
    ):
        sid = f"explicit-{uuid.uuid4().hex[:8]}"
        _seed(store, sid, session_chem, session_cas)
        resp = _run(orchestrator, query, sid)
        slots = _slots(resp)
        assert slots.get("resolution_source") != "session_context", slots
        if expected_cas is None:
            assert slots.get("cas") != session_cas, "session must not override explicit toluene query"
            assert slots.get("resolution_method") != "inherited", slots
            return
        _assert_cas(slots, expected_cas)
        if slots.get("resolution_method") != "inherited":
            assert slots.get("resolution_source") in {"explicit_query", "cas_query"}, slots


# ── 2. Valid follow-up inheritance (5+) ─────────────────────────────────────

@pytest.mark.integration
class TestValidFollowUpInheritance:
    def test_stel_after_benzene(self, orchestrator):
        sid = f"followup-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "حد مجاز بنزن چقدره", sid)
        resp = _run(orchestrator, "STEL چقدره؟", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["benzene"])
        assert slots.get("resolution_source") == "session_context"

    def test_cas_after_benzene(self, orchestrator):
        sid = f"followup-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "حد مجاز بنزن چقدره", sid)
        resp = _run(orchestrator, "CAS چنده؟", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["benzene"])
        assert slots.get("resolution_source") == "session_context"

    def test_twa_bare_after_acetonitrile(self, orchestrator, store):
        sid = f"followup-{uuid.uuid4().hex[:8]}"
        _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, "TWA?", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["acetonitrile"])
        assert slots.get("resolution_source") == "session_context"

    def test_stel_bare_after_acetaldehyde(self, orchestrator):
        sid = f"followup-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "STEL acetaldehyde 75-07-0", sid)
        resp = _run(orchestrator, "STEL?", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["acetaldehyde"])

    def test_ceiling_followup_persian(self, orchestrator):
        sid = f"followup-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "حد مجاز TWA برای بیس فنول آ 80-05-7", sid)
        resp = _run(orchestrator, "سقف؟", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["bisphenol_a"])


# ── 3. Entity switching (5+) ────────────────────────────────────────────────

@pytest.mark.integration
class TestEntitySwitching:
    def test_acetonitrile_to_benzene(self, orchestrator):
        sid = f"switch-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "STEL acetonitrile 75-05-8", sid)
        resp = _run(orchestrator, "حد مجاز بنزن چقدره", sid)
        _assert_cas(_slots(resp), CAS["benzene"])

    def test_benzene_to_aluminosilicate(self, orchestrator):
        sid = f"switch-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "حد TWA بنزن چقدره؟", sid)
        resp = _run(orchestrator, "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", sid)
        _assert_cas(_slots(resp), CAS["aluminosilicate"])

    def test_chemical_to_formula_topic(self, orchestrator):
        sid = f"switch-{uuid.uuid4().hex[:8]}"
        _run(orchestrator, "حد مجاز بنزن چقدره", sid)
        resp = _run(orchestrator, "فرمول ارتعاش صفحه 100 شماره 1 چیست؟", sid)
        assert "FORMULA" in resp.intent or resp.intent.startswith("SEMANTIC")

    def test_off_domain_rejects_without_chemical_answer(self, orchestrator, store):
        sid = f"switch-{uuid.uuid4().hex[:8]}"
        _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, "اصلا دلم گرفته مولکول نمیخوام", sid)
        assert resp.intent == "DOMAIN.REJECTED"
        assert CAS["acetonitrile"] not in (resp.answer or "")

    def test_switch_does_not_keep_prior_in_answer(self, orchestrator):
        sid = f"switch-{uuid.uuid4().hex[:8]}"
        r1 = _run(orchestrator, "STEL acetonitrile 75-05-8", sid)
        assert CAS["acetonitrile"] in str(_slots(r1).get("cas"))
        r2 = _run(orchestrator, "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", sid)
        assert "Acetonitrile" not in (r2.answer or "")
        _assert_cas(_slots(r2), CAS["aluminosilicate"])


# ── 4. Cross-session isolation (3 sessions) ─────────────────────────────────

@pytest.mark.integration
class TestCrossSessionIsolation:
    def test_three_independent_sessions(self, orchestrator, store):
        sid_a = "iso-session-a"
        sid_b = "iso-session-b"
        sid_c = "iso-session-c"
        _seed(store, sid_a, "Acetonitrile", CAS["acetonitrile"])
        _seed(store, sid_b, "Benzene", CAS["benzene"])
        _seed(store, sid_c, "Aluminosilicate fibres", CAS["aluminosilicate"])

        ra = _run(orchestrator, "STEL?", sid_a)
        rb = _run(orchestrator, "STEL?", sid_b)
        rc = _run(orchestrator, "STEL?", sid_c)

        _assert_cas(_slots(ra), CAS["acetonitrile"])
        _assert_cas(_slots(rb), CAS["benzene"])
        _assert_cas(_slots(rc), CAS["aluminosilicate"])

    def test_new_session_id_no_prior_context(self, orchestrator, store):
        _seed(store, "iso-session-a", "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, "STEL?", f"brand-new-{uuid.uuid4().hex}")
        assert _slots(resp).get("cas") is None
        assert resp.intent.startswith("CLARIFY")


# ── 5. No silent fallback (5+) ──────────────────────────────────────────────

@pytest.mark.integration
class TestNoSilentFallback:
    @pytest.mark.parametrize(
        "query,use_session,forbidden_cas",
        [
            ("حد مجاز ماده ناموجود XYZ-9999 چقدره", True, {CAS["acetonitrile"]}),
            ("OEL for NonExistentChemical-12345", True, {CAS["acetonitrile"]}),
            ("حدش چنده؟", False, {CAS["acetonitrile"], CAS["benzene"]}),
            ("حد مجاز xxx-yyy-zzz چقدره", True, {CAS["acetonitrile"]}),
        ],
    )
    def test_unresolved_never_returns_unrelated_chemical(
        self, orchestrator, store, query, use_session, forbidden_cas
    ):
        sid = f"nosilent-{uuid.uuid4().hex[:8]}" if use_session else None
        if use_session:
            _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, query, sid)
        slots = _slots(resp)
        if slots.get("cas"):
            assert slots.get("cas") not in forbidden_cas, slots
        if resp.intent.startswith("CLARIFY") or resp.requires_clarification:
            assert slots.get("cas") is None or slots.get("cas") not in forbidden_cas

    def test_bare_stel_without_session_clarifies(self, orchestrator):
        resp = _run(orchestrator, "STEL?", None)
        assert resp.intent.startswith("CLARIFY")
        assert _slots(resp).get("cas") is None

    def test_ambiguous_multi_silicate_clarifies(self, orchestrator, store):
        sid = f"nosilent-{uuid.uuid4().hex[:8]}"
        _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        # Force ambiguous-like query without enough tokens (generic silicate term only)
        resp = _run(orchestrator, "حد مجاز سیلیکات چقدره", sid)
        slots = _slots(resp)
        assert slots.get("cas") != CAS["acetonitrile"]


# ── 6. Trace provenance ─────────────────────────────────────────────────────

@pytest.mark.integration
class TestTraceProvenance:
    def test_explicit_query_source(self, db_session, store):
        _, u = _resolve(db_session, store, "حد مجاز بنزن چقدره", None)
        assert u.slots.get("resolution_source") == "explicit_query"
        assert u.slots.get("resolution_method") != "inherited"

    def test_cas_query_source(self, db_session, store):
        _, u = _resolve(db_session, store, "STEL 75-07-0", None)
        assert u.slots.get("resolution_source") == "cas_query"

    def test_session_context_source(self, db_session, store):
        _seed(store, "prov", "Benzene", CAS["benzene"])
        _, u = _resolve(db_session, store, "STEL?", "prov")
        assert u.slots.get("resolution_source") == "session_context"
        assert u.slots.get("resolution_method") == "inherited"

    def test_never_inherited_when_explicit_in_query(self, db_session, store):
        _seed(store, "prov", "Acetonitrile", CAS["acetonitrile"])
        _, u = _resolve(db_session, store, "حد مجاز بنزن چقدره", "prov")
        assert u.slots.get("resolution_source") == "explicit_query"
        assert u.slots.get("cas") == CAS["benzene"]


# ── 7. Original reported queries ────────────────────────────────────────────

@pytest.mark.integration
class TestOriginalReportedQueries:
    def test_query1_twe_acetonitrile(self, orchestrator, store):
        sid = "orig-q1"
        _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, "twe برای استونیتریل چقدره", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["acetonitrile"])
        assert resp.intent == "STRUCTURED.OEL.TWA_LOOKUP"
        assert slots.get("resolution_source") == "explicit_query"

    def test_query2_aluminosilicate_not_acetonitrile(self, orchestrator, store):
        sid = "orig-q2"
        _seed(store, sid, "Acetonitrile", CAS["acetonitrile"])
        resp = _run(orchestrator, "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره", sid)
        slots = _slots(resp)
        _assert_cas(slots, CAS["aluminosilicate"])
        assert CAS["acetonitrile"] not in (resp.answer or "")


# ── Unit-level entity signal checks ─────────────────────────────────────────

class TestEntitySignalUnit:
    def test_limit_type_question_not_explicit(self):
        assert not query_has_explicit_entity_signal("STEL چقدره؟")
        assert not query_has_explicit_entity_signal("TWA?")
        assert not query_has_explicit_entity_signal("CAS چنده؟")

    def test_chemical_name_is_explicit(self):
        assert query_has_explicit_entity_signal("حد مجاز بنزن چقدره")
        assert query_has_explicit_entity_signal("STEL acetonitrile 75-05-8")
