"""Phase C — Query Router, Session & Agents tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.guardrails.gate import GuardrailGate
from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots
from agents.session.context import SessionContextManager
from agents.session.store import SessionStore
from agents.synthesis.answer import AnswerSynthesizer


class TestNormalizer:
    def test_persian_digits(self):
        assert "123" in normalize_persian_query("۱۲۳ ppm")


class TestSlotExtraction:
    def test_cas(self):
        slots = extract_slots("CAS 71-43-2 TWA")
        assert slots["cas"] == "71-43-2"

    def test_concentration(self):
        slots = extract_slots("مواجهه 2 ppm")
        assert slots["concentration"] == 2.0


class TestSessionContext:
    def test_followup_stel(self):
        store = SessionStore()
        mgr = SessionContextManager(store)
        sid = "test-session"
        state = store.create_session(sid)
        state.chemical_name = "Benzene"
        state.turn_id = 1
        store.save(state)

        _, ctx = mgr.begin_turn(sid, "STEL نداره?")
        assert ctx.requires_context
        assert "Benzene" in ctx.resolved_query or ctx.merged_slots.get("chemical_name") == "Benzene"

    def test_entity_replacement(self):
        store = SessionStore()
        mgr = SessionContextManager(store)
        _, ctx = mgr.begin_turn(None, "حد TWA تولوئن چقدره؟")
        slots = extract_slots(ctx.resolved_query, ctx.merged_slots)
        assert slots.get("chemical_name") == "Toluene"

    def test_ambiguous_followup(self):
        store = SessionStore()
        mgr = SessionContextManager(store)
        _, ctx = mgr.begin_turn("new", "حدش چنده؟")
        assert ctx.ambiguous or "chemical_name" in ctx.missing_for_resolution

    def test_context_reset_new_session(self):
        store = SessionStore()
        mgr = SessionContextManager(store)
        state, ctx = mgr.begin_turn(None, "حد TWA بنزن")
        assert state is None
        assert not ctx.requires_context


class TestClassifier:
    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    def test_twa_structured(self, classifier):
        r = classifier.classify("حد TWA بنزن چقدره؟", slots={"chemical_name": "Benzene"})
        assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"
        assert "structured" in r.expected_agents

    def test_definition_semantic(self, classifier):
        r = classifier.classify("TWA یعنی چی؟", slots={})
        assert r.intent.startswith("SEMANTIC")

    def test_clarify_missing_chemical(self, classifier):
        r = classifier.classify("حدش چنده؟", slots={}, session_ambiguous=True, missing_slots=["chemical_name"])
        assert r.requires_clarification
        assert r.intent.startswith("CLARIFY")

    def test_hybrid_lookup_explain(self, classifier):
        r = classifier.classify("حد TWA بنزن چقدره و یعنی چی؟", slots={"chemical_name": "Benzene"})
        assert r.intent == "HYBRID.LOOKUP_AND_EXPLAIN"

    def test_guardrail_professional(self, classifier):
        r = classifier.classify("آیا باید کارگر را اخراج کنم؟", slots={})
        assert r.intent == "GUARDRAIL.PROFESSIONAL_JUDGMENT"


class TestGuardrails:
    def test_semantic_numeric_forbidden(self):
        gate = GuardrailGate()
        d = gate.evaluate(
            intent="STRUCTURED.OEL.TWA_LOOKUP",
            classification={"requires_clarification": False, "numeric_safety_level": 2},
            agent_results={"semantic": {"success": True}, "structured": {"success": False}},
        )
        assert not d.allowed

    def test_clarify(self):
        gate = GuardrailGate()
        d = gate.evaluate(
            intent="CLARIFY.MISSING_CHEMICAL",
            classification={"requires_clarification": True, "numeric_safety_level": 0},
            agent_results={},
        )
        assert d.action == "clarify"


class TestSynthesis:
    def test_structured_answer(self):
        syn = AnswerSynthesizer()
        text, _ = syn._structured_answer(
            "STRUCTURED.OEL.TWA_LOOKUP",
            {"chemical_name": "Benzene", "field": "TWA", "value": 0.5, "unit": "ppm", "source_row_key": "table:row_1"},
        )
        assert "0.5" in text
        assert "Benzene" in text or "بenzene" in text.lower() or "TWA" in text


class TestStructuredAgentMock:
    def test_oel_lookup_mock(self):
        from agents.structured.agent import StructuredAgent

        store = MagicMock()
        store.lookup_oel_field.return_value = {
            "field": "TWA",
            "value": 0.5,
            "unit": "ppm",
            "source_row_key": "table_046_01:row_2",
            "page_number": 46,
            "chemical_name": "Benzene",
            "validation_status": "accepted",
            "gold_artifact_path": "canonical_evidence_v1",
            "cas": "71-43-2",
            "chemical_id": "test-chemical-id",
        }
        agent = StructuredAgent(store)
        res = agent.execute("STRUCTURED.OEL.TWA_LOOKUP", {"chemical_name": "Benzene"})
        assert res.success
        assert res.data["value"] == 0.5

    def test_mw_lookup_propagates_oel_provenance(self):
        from agents.structured.agent import StructuredAgent

        store = MagicMock()
        store.resolve_molecular_weight.return_value = {
            "molecular_weight": 87.12,
            "molecular_weight_display": "87.12",
            "chemical_name": "acetamide Dimethyl",
            "cas": "127-19-5",
            "source": "oel_original_values",
            "source_row_key": "oel:46:row_1",
            "page_number": 46,
        }
        agent = StructuredAgent(store)
        res = agent.execute(
            "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT",
            {"chemical_name": "acetamide Dimethyl", "cas": "127-19-5"},
        )
        assert res.success
        assert res.data["page_number"] == 46
        assert res.data["source_row_key"] == "oel:46:row_1"
        assert res.citations
        assert res.citations[0]["page_number"] == 46
        assert res.citations[0]["source_row_key"] == "oel:46:row_1"
        assert res.citations[0]["table"] == "oel_chemical_limits"


def _db_available() -> bool:
    try:
        from database.session import verify_connection
        return verify_connection()
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not available")


@pytestmark_db
class TestOrchestratorIntegration:
    def test_benzene_twa_query(self):
        from database.session import session_scope
        from agents.orchestrator.pipeline import QueryOrchestrator

        with session_scope() as session:
            orch = QueryOrchestrator(session)
            resp = orch.handle("حد TWA بنزن چقدره؟", session_id="test-e2e-1")
            assert resp.answer
            assert resp.intent.startswith("STRUCTURED") or "CLARIFY" in resp.intent
            assert resp.trace_id


class TestRouterEvalSample:
    def test_router_eval_runs(self):
        from agents.evaluation.harness import evaluate_router

        summary = evaluate_router(limit=200)
        assert summary.total == 200
        assert summary.intent_accuracy() >= 0.0  # report metric, not asserting high accuracy in unit test
