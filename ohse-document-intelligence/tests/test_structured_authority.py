"""Tests for canonical-only structured authority and wrong-chemical invariants."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from agents.routing.chemical_registry_cache import reset_chemical_registry_cache
from agents.routing.normalizer import strip_limit_type_prefix
from agents.structured.authority import (
    CANONICAL_GOLD_ARTIFACT_PATH,
    validate_authoritative_oel_row,
)
from agents.structured.integrity import find_duplicate_canonical_limit_types
from agents.structured.store import PostgresStructuredStore


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_chemical_registry_cache()
    yield
    reset_chemical_registry_cache()


class TestLimitTypePrefixParsing:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("TWA benzene", "benzene"),
            ("twa for benzene", "benzene"),
            ("STEL toluene", "toluene"),
            ("CEILING ammonia", "ammonia"),
            ("TWA بنزن", "بنزن"),
            ("benzene TWA", "benzene TWA"),
        ],
    )
    def test_strip_limit_type_prefix(self, query: str, expected: str):
        assert strip_limit_type_prefix(query) == expected


class TestAuthorityValidator:
    def test_accepts_canonical_row(self):
        validate_authoritative_oel_row(
            {
                "chemical_id": "abc",
                "cas": "71-43-2",
                "validation_status": "accepted",
                "gold_artifact_path": CANONICAL_GOLD_ARTIFACT_PATH,
            },
            expected_chemical_id="abc",
            expected_cas="71-43-2",
        )

    def test_rejects_legacy_status(self):
        with pytest.raises(Exception, match="validation_status"):
            validate_authoritative_oel_row(
                {
                    "chemical_id": "abc",
                    "cas": "67-64-1",
                    "validation_status": "legacy_reference",
                    "gold_artifact_path": CANONICAL_GOLD_ARTIFACT_PATH,
                }
            )

    def test_rejects_entity_mismatch(self):
        with pytest.raises(Exception, match="chemical_id"):
            validate_authoritative_oel_row(
                {
                    "chemical_id": "wrong",
                    "cas": "71-43-2",
                    "validation_status": "accepted",
                    "gold_artifact_path": CANONICAL_GOLD_ARTIFACT_PATH,
                },
                expected_chemical_id="expected",
            )


@pytest.mark.integration
class TestCanonicalLimitTypeIntegrity:
    def test_no_duplicate_canonical_limit_types(self):
        """Each (chemical_id, limit_type) may appear at most once across canonical rows."""
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            duplicates = find_duplicate_canonical_limit_types(session)
            assert duplicates == [], (
                "True duplicate canonical limit types detected "
                "(complementary rows like Butenal TWA/STEL split must not trigger this): "
                f"{duplicates[:5]}"
            )
        finally:
            session.close()


@pytest.mark.integration
class TestCanonicalOnlyStructuredStore:
    def test_legacy_only_acetone_returns_no_rows(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            store = PostgresStructuredStore(session)
            rows = store.get_oel_by_cas("67-64-1")
            assert rows == []
            assert store.lookup_oel_field(cas="67-64-1", oel_type="TWA") is None
        finally:
            session.close()

    def test_canonical_benzene_still_returns_value(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            store = PostgresStructuredStore(session)
            result = store.lookup_oel_field(cas="71-43-2", oel_type="TWA")
            assert result is not None
            assert result["gold_artifact_path"] == CANONICAL_GOLD_ARTIFACT_PATH
            assert result["validation_status"] == "accepted"
            assert result["chemical_id"]
            assert result["value"] is not None
        finally:
            session.close()

    def test_butenal_complementary_rows_select_by_limit_type(self):
        from database.session import SessionLocal

        session = SessionLocal()
        try:
            store = PostgresStructuredStore(session)
            twa = store.lookup_oel_field(cas="4170-30-3", oel_type="TWA")
            stel = store.lookup_oel_field(cas="4170-30-3", oel_type="STEL")
            assert twa is not None and twa["value"] is not None
            assert stel is not None and stel["value"] is not None
            assert twa["source_row_key"] != stel["source_row_key"]
        finally:
            session.close()


STATIC_CORRECTNESS_MATRIX = [
    pytest.param(
        {
            "query": "twe برای استونیتریل چقدره",
            "session_id": "matrix-acetonitrile",
            "expect_intent": "STRUCTURED.OEL.TWA_LOOKUP",
            "expect_cas": "75-05-8",
            "expect_structured_success": True,
        },
        id="acetonitrile_twa_persian",
    ),
    pytest.param(
        {
            "query": "حد مجاز فیبرهای سیلیکات آلومینیوم چقدره",
            "session_id": "matrix-aluminosilicate",
            "expect_cas": "142844-00-6",
            "expect_structured_success": True,
            "forbidden_cas": ["75-05-8"],
        },
        id="aluminosilicate_persian",
    ),
    pytest.param(
        {
            "query": "آمینو بوتانول twa چی میشه",
            "session_id": "matrix-amino-butanol",
            "expect_cas": "96-20-8",
            "expect_intent": "STRUCTURED.OEL.TWA_LOOKUP",
            "expect_structured_success": True,
            "expect_answer_contains": ["1.0"],
        },
        id="amino_butanol_twa",
    ),
    pytest.param(
        {
            "query": "TWA acetone",
            "session_id": "matrix-acetone-legacy-only",
            "expect_cas": "67-64-1",
            "expect_structured_success": False,
            "expect_no_data": True,
            "expect_no_data_reason": "legacy_only_pending_promotion",
        },
        id="legacy_only_acetone_no_data",
    ),
    pytest.param(
        {
            "query": "TWA",
            "session_id": "matrix-bare-twa",
            "expect_intent": "CLARIFY.MISSING_CHEMICAL",
            "expect_structured_success": False,
        },
        id="bare_twa_clarify",
    ),
    pytest.param(
        {
            "query": "TWA benzene",
            "session_id": "matrix-benzene-en-order",
            "expect_cas": "71-43-2",
            "expect_intent": "STRUCTURED.OEL.TWA_LOOKUP",
            "expect_structured_success": True,
        },
        id="benzene_english_word_order",
    ),
    pytest.param(
        {
            "query": "TWA بنزن چقدره",
            "session_id": "matrix-benzene-fa",
            "expect_cas": "71-43-2",
            "expect_structured_success": True,
        },
        id="benzene_persian",
    ),
    pytest.param(
        {
            "query": "CAS 71-43-2 TWA",
            "session_id": "matrix-benzene-cas",
            "expect_cas": "71-43-2",
            "expect_structured_success": True,
        },
        id="benzene_cas_lookup",
    ),
    pytest.param(
        {
            "query": "TWA for Butenal",
            "session_id": "matrix-butenal-twa",
            "expect_cas": "4170-30-3",
            "expect_oel_type": "TWA",
            "expect_structured_success": True,
        },
        id="butenal_twa_complementary_row",
    ),
    pytest.param(
        {
            "query": "STEL Butenal",
            "session_id": "matrix-butenal-stel",
            "expect_cas": "4170-30-3",
            "expect_oel_type": "STEL",
            "expect_structured_success": True,
        },
        id="butenal_stel_complementary_row",
    ),
    pytest.param(
        {
            "query": "TWA استون چقدره",
            "session_id": "matrix-acetone-fa-legacy",
            "expect_cas": "67-64-1",
            "expect_structured_success": False,
            "expect_no_data": True,
        },
        id="legacy_only_acetone_persian",
    ),
    pytest.param(
        {
            "query": "TWA Acetic acid",
            "session_id": "matrix-acetic-legacy",
            "expect_cas": "64-19-7",
            "expect_structured_success": False,
            "expect_no_data": True,
        },
        id="legacy_only_acetic_acid",
    ),
    pytest.param(
        {
            "query": "TWA Acetamide",
            "session_id": "matrix-acetamide-legacy",
            "expect_cas": "60-35-5",
            "expect_structured_success": False,
            "expect_no_data": True,
            "expect_no_data_reason": "legacy_only_pending_promotion",
        },
        id="legacy_only_acetamide",
    ),
    pytest.param(
        {
            "query": "STEL CAS 100-42-5",
            "session_id": "matrix-styrene-cas-stel",
            "expect_cas": "100-42-5",
            "expect_oel_type": "STEL",
            "expect_structured_success": False,
            "expect_no_data": True,
        },
        id="styrene_no_canonical_stel",
    ),
]


def _ascii_name(name: str | None) -> bool:
    if not name:
        return False
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9 \-(),.']{2,60}$", name.strip()))


def _build_db_matrix_cases() -> list:
    """Load legacy-only and canonical samples from DB for parametrized regression."""
    try:
        from database.session import SessionLocal
    except Exception:
        return []

    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        session.close()
        return []

    cases: list = []
    try:
        legacy_rows = session.execute(
            text(
                """
                SELECT cr.cas, cr.english_name
                FROM chemical_registry cr
                WHERE EXISTS (
                  SELECT 1 FROM oel_chemical_limits o
                  WHERE o.chemical_id = cr.id
                    AND o.validation_status = 'legacy_reference'
                )
                AND NOT EXISTS (
                  SELECT 1 FROM oel_chemical_limits o
                  WHERE o.chemical_id = cr.id
                    AND o.validation_status = 'accepted'
                    AND o.gold_artifact_path = 'canonical_evidence_v1'
                )
                ORDER BY cr.english_name
                """
            )
        ).fetchall()

        step = max(1, len(legacy_rows) // 12)
        for idx, (cas, name) in enumerate(legacy_rows):
            if idx % step != 0 or not cas:
                continue
            safe = re.sub(r"[^a-z0-9]+", "-", (name or cas).lower())[:30]
            cases.append(
                pytest.param(
                    {
                        "query": f"TWA CAS {cas}",
                        "session_id": f"matrix-legacy-{safe}",
                        "expect_cas": cas,
                        "expect_structured_success": False,
                        "expect_no_data": True,
                    },
                    id=f"legacy_only_{safe}",
                )
            )

        canonical_rows = session.execute(
            text(
                """
                SELECT DISTINCT cr.cas, cr.english_name
                FROM chemical_registry cr
                JOIN oel_chemical_limits o ON o.chemical_id = cr.id
                WHERE o.validation_status = 'accepted'
                  AND o.gold_artifact_path = 'canonical_evidence_v1'
                  AND o.twa IS NOT NULL
                  AND cr.cas IS NOT NULL
                ORDER BY cr.english_name
                """
            )
        ).fetchall()

        canonical_filtered = [
            (cas, name) for cas, name in canonical_rows if _ascii_name(name) and cas
        ]
        step = max(1, len(canonical_filtered) // 15)
        for idx, (cas, name) in enumerate(canonical_filtered):
            if idx % step != 0:
                continue
            safe = re.sub(r"[^a-z0-9]+", "-", name.lower())[:30]
            cases.append(
                pytest.param(
                    {
                        "query": f"TWA CAS {cas}",
                        "session_id": f"matrix-canonical-{safe}",
                        "expect_cas": cas,
                        "expect_structured_success": True,
                    },
                    id=f"canonical_{safe}",
                )
            )
    finally:
        session.close()

    return cases


CORRECTNESS_MATRIX = STATIC_CORRECTNESS_MATRIX + _build_db_matrix_cases()


@pytest.mark.integration
@pytest.mark.parametrize("case", CORRECTNESS_MATRIX)
def test_correctness_matrix(case: dict):
    from agents.orchestrator.pipeline import QueryOrchestrator
    from agents.session.store import SessionStore
    from database.session import SessionLocal

    session = SessionLocal()
    try:
        polluted_store = SessionStore()
        polluted = polluted_store.create_session(f"polluted-{case['session_id']}")
        polluted.chemical_name = "Acetonitrile"
        polluted.cas = "75-05-8"
        polluted.turn_id = 1
        polluted_store.save(polluted)

        orch = QueryOrchestrator(session, session_store=SessionStore())
        resp = orch.handle(case["query"], session_id=case["session_id"])

        trace = resp.metadata.get("trace", {})
        slots = trace.get("slots", {})

        if case.get("expect_intent"):
            assert resp.intent == case["expect_intent"]
        if case.get("expect_cas"):
            assert slots.get("cas") == case["expect_cas"], slots
        if case.get("expect_oel_type"):
            assert slots.get("oel_type") == case["expect_oel_type"], slots
        if case.get("forbidden_cas"):
            assert slots.get("cas") not in case["forbidden_cas"]

        if case.get("expect_structured_success") is True:
            assert trace.get("guardrail_decision") == "pass", trace
            assert resp.answer
            store = PostgresStructuredStore(session)
            lookup = store.lookup_oel_field(
                cas=case.get("expect_cas") or slots.get("cas"),
                chemical_id=slots.get("chemical_id"),
                oel_type=slots.get("oel_type") or case.get("expect_oel_type") or "TWA",
            )
            assert lookup is not None, lookup
            assert lookup.get("gold_artifact_path") == CANONICAL_GOLD_ARTIFACT_PATH
            assert lookup.get("validation_status") == "accepted"
            if case.get("expect_cas"):
                assert lookup.get("cas") == case["expect_cas"]
            if slots.get("chemical_id"):
                assert lookup.get("chemical_id") == slots.get("chemical_id")
        elif case.get("expect_structured_success") is False:
            assert trace.get("guardrail_decision") in {"no_data", "clarify", "refuse"}, trace

        if case.get("expect_no_data"):
            assert trace.get("guardrail_decision") == "no_data", trace
            assert "500" not in (resp.answer or "")
            if case.get("expect_no_data_reason"):
                assert (
                    resp.metadata.get("no_data_reason") == case["expect_no_data_reason"]
                    or trace.get("no_data_reason") == case["expect_no_data_reason"]
                )

        if case.get("expect_answer_contains"):
            answer = resp.answer or ""
            assert any(token in answer for token in case["expect_answer_contains"]), answer

        polluted_resp = QueryOrchestrator(session, session_store=polluted_store).handle(
            case["query"],
            session_id=f"polluted-{case['session_id']}",
        )
        polluted_slots = polluted_resp.metadata.get("trace", {}).get("slots", {})
        if case.get("expect_cas"):
            assert polluted_slots.get("cas") == case["expect_cas"], polluted_slots
    finally:
        session.close()
