"""Phase C.3 tests — retrieval pipeline, chemical resolver, query understanding."""

from __future__ import annotations

import pytest

from agents.routing.chemical_resolver import ChemicalResolver, _reverse_descriptor_phrase
from agents.routing.query_understanding import enhance_normalization, classify_query_type, QueryType
from agents.hybrid.plans import get_hybrid_plan, agents_for_hybrid
from retrieval.lexical_index import LexicalIndex, _tokenize
from retrieval.pipeline import _rrf_fuse, RetrievalMode
from retrieval.reranker import LexicalReranker, RankedCandidate, NoOpReranker
from retrieval.formula_inventory import EXECUTABLE_FORMULAS, is_executable


class TestChemicalResolverPatterns:
    def test_reverse_acid_acetic(self):
        variants = _reverse_descriptor_phrase("acid Acetic")
        assert any("Acetic" in v for v in variants)

    def test_reverse_chloride(self):
        variants = _reverse_descriptor_phrase("choloride Allyl")
        assert any("Allyl" in v for v in variants)


class TestQueryUnderstanding:
    def test_abbreviation_normalization(self):
        q = enhance_normalization("میانگین وزنی بنزن")
        assert "TWA" in q

    def test_classify_numeric_lookup(self):
        t = classify_query_type("حد TWA Benzene", {"chemical_name": "Benzene", "oel_type": "TWA"})
        assert t == QueryType.NUMERIC_LOOKUP

    def test_classify_definition(self):
        t = classify_query_type("TWA یعنی چی؟", {})
        assert t == QueryType.DEFINITION

    def test_classify_formula(self):
        t = classify_query_type("ahv را محاسبه کن", {"variables": {"ahw_1": 1.0}})
        assert t == QueryType.FORMULA_CALC


class TestRetrievalPipeline:
    def test_rrf_fusion(self):
        list_a = [{"chunk_id": "a", "content": "x", "score": 0.9}, {"chunk_id": "b", "content": "y", "score": 0.8}]
        list_b = [{"chunk_id": "b", "content": "y", "score": 0.7}, {"chunk_id": "c", "content": "z", "score": 0.6}]
        fused = _rrf_fuse([list_a, list_b])
        ids = [f["chunk_id"] for f in fused]
        assert "b" in ids
        assert ids[0] in {"a", "b"}

    def test_lexical_reranker(self):
        cands = [
            RankedCandidate("a", "unrelated", 0.99),
            RankedCandidate("b", "TWA یعنی میانگین وزنی زمانی", 0.7),
        ]
        out = LexicalReranker().rerank("TWA میانگین وزنی", cands, top_k=1)
        assert out[0].chunk_id == "b"

    def test_tokenize_persian(self):
        tokens = _tokenize("حد مجاز بنزن")
        assert "بenzene" not in tokens  # Persian tokens preserved
        assert "مجاز" in tokens or "حد" in tokens


class TestHybridPlans:
    def test_lookup_and_explain_plan(self):
        plan = get_hybrid_plan("HYBRID.LOOKUP_AND_EXPLAIN")
        assert plan is not None
        assert "structured" in plan.agents
        assert "semantic" in plan.agents

    def test_compare_minimal_agents(self):
        agents = agents_for_hybrid("HYBRID.LOOKUP_COMPARE_EXPLAIN", "مواجهه 2 ppm", {"concentration": 2})
        assert "structured" in agents
        assert "formula" in agents


class TestFormulaInventory:
    def test_executable_set(self):
        assert "formula_240_01" in EXECUTABLE_FORMULAS
        assert is_executable("formula_240_01")


class TestClassifierFollowup:
    def test_stel_short_with_chemical(self):
        from agents.routing.classifier import IntentClassifier
        c = IntentClassifier()
        r = c.classify("STEL?", slots={"chemical_name": "Benzene"})
        assert r.intent == "STRUCTURED.OEL.STEL_LOOKUP"

    def test_twa_short_with_chemical(self):
        from agents.routing.classifier import IntentClassifier
        c = IntentClassifier()
        r = c.classify("TWA?", slots={"chemical_name": "Benzene"})
        assert r.intent == "STRUCTURED.OEL.TWA_LOOKUP"


class TestSessionContext:
    def test_stel_followup_resolution(self):
        from agents.session.context import SessionContextManager
        from agents.session.store import SessionStore, SessionState

        store = SessionStore()
        state = store.create_session("test-s1")
        state.chemical_name = "Benzene"
        state.turn_id = 1
        store.save(state)

        mgr = SessionContextManager(store)
        state, ctx = mgr.begin_turn("test-s1", "STEL?")
        assert "Benzene" in ctx.resolved_query or ctx.inherited_slots.get("chemical_name") == "Benzene"
