"""Regression tests for query latency guards and unified error responses."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.routing.chemical_resolver import ChemicalResolver, _modifier_consistent
from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots
from api.errors import RequestTimeoutError
from api.main import app
from api.routers.query import QueryRequest, execute_query
from database.models import ChemicalRegistry


def _classify(query: str):
    c = IntentClassifier()
    n = normalize_persian_query(query)
    slots = extract_slots(n)
    return c.classify(n, slots=slots)


class TestDimethylSulfideRouting:
    def test_twa_yes_no_question_intent(self):
        r = _classify("ایا دی متیل سولفید twa داره؟")
        assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"

    def test_di_methyl_requires_dimethyl_in_english_name(self):
        benzene = MagicMock(spec=ChemicalRegistry)
        benzene.english_name = "Benzene"
        benzene.persian_name = "بنزن"
        dms = MagicMock(spec=ChemicalRegistry)
        dms.english_name = "sulfide Dimethyl"
        dms.persian_name = "دی"
        q = "ایا دی متیل سولفید twa داره؟"
        assert not _modifier_consistent(q, benzene)
        assert _modifier_consistent(q, dms)


class TestUnifiedErrorResponse:
    def test_timeout_returns_same_schema(self):
        with patch("api.routers.query._query_executor") as mock_pool:
            from concurrent.futures import TimeoutError as FuturesTimeoutError

            future = MagicMock()
            future.result.side_effect = FuturesTimeoutError()
            mock_pool.submit.return_value = future

            resp = execute_query(None, QueryRequest(query="حد TWA بنزن", session_id="t1"))
            assert resp.success is False
            assert resp.intent == "SYSTEM.TIMEOUT"
            assert resp.error is not None
            assert resp.error.code == "REQUEST_TIMEOUT"
            assert resp.trace_id

    def test_validation_error_http_envelope(self):
        client = TestClient(app)
        r = client.post("/query", json={"query": ""})
        assert r.status_code == 422
        body = r.json()
        assert body["success"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "trace_id" in body

    def test_gate_reject_includes_error_block(self):
        with patch("api.routers.query._run_query_in_thread") as mock_run:
            mock_run.return_value = MagicMock(
                to_dict=lambda: {
                    "success": False,
                    "answer": "این سؤال مربوط به بهداشت و ایمنی شغلی (OHSE) نیست.",
                    "intent": "DOMAIN.REJECTED",
                    "agents": [],
                    "citations": [],
                    "confidence": 0.85,
                    "trace_id": "abc-123",
                    "session_id": "s1",
                    "requires_clarification": False,
                    "gate_decision": "reject",
                    "error": {
                        "code": "GATE_REJECT",
                        "message": "rejected",
                        "message_fa": "این سؤال مربوط به بهداشت و ایمنی شغلی (OHSE) نیست.",
                        "details": {},
                    },
                    "metadata": {},
                }
            )
            resp = execute_query(None, QueryRequest(query="فوتبال", session_id="s1"))
            assert resp.success is False
            assert resp.gate_decision == "reject"
            assert resp.error is not None


class TestResolverPerformanceGuard:
    @patch("agents.routing.chemical_resolver.get_accepted_chemicals")
    def test_resolve_does_not_use_sequence_matcher(self, mock_get):
        chemicals = []
        for i in range(300):
            c = MagicMock(spec=ChemicalRegistry)
            c.english_name = f"compound Methyl {i}"
            c.persian_name = f"p{i}"
            c.cas = f"{i:02d}-00-0"
            c.aliases = {}
            chemicals.append(c)
        dms = MagicMock(spec=ChemicalRegistry)
        dms.english_name = "sulfide Dimethyl"
        dms.persian_name = "دی"
        dms.cas = "75-18-3"
        dms.aliases = {"fa": ["دی"]}
        chemicals.append(dms)
        mock_get.return_value = chemicals
        session = MagicMock()
        resolver = ChemicalResolver(session)
        res = resolver.resolve("ایا دی متیل سولفید twa داره؟")
        assert res.canonical_name == "sulfide Dimethyl"
