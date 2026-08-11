"""Phase C.2 tests — formula dataset, conversational context, reranker, retrieval eval."""

from __future__ import annotations

import pytest

from config.settings import PROJECT_ROOT
from knowledge.calculation import evaluate_vibration_daily_exposure
from retrieval.eval_conversational_builder import build_conversational_eval_dataset
from retrieval.eval_formula_builder import build_formula_eval_dataset
from retrieval.eval_gold_loader import load_gold_corpus
from retrieval.reranker import (
    LexicalReranker,
    NoOpReranker,
    RankedCandidate,
    candidates_from_search_results,
    get_available_rerankers,
)
from retrieval.eval_metrics import recall_at_k, mrr, ndcg_at_k
from retrieval.chunk_quality_analysis import analyze_chunk_quality
from agents.routing.classifier import IntentClassifier


@pytest.fixture
def corpus():
    return load_gold_corpus()


@pytest.fixture
def classifier():
    return IntentClassifier()


class TestFormulaDataset:
    def test_minimum_count(self, corpus):
        records = build_formula_eval_dataset(corpus)
        assert len(records) >= 300

    def test_ground_truth_from_registry(self, corpus):
        records = build_formula_eval_dataset(corpus)
        calc_records = [r for r in records if r.intent == "FORMULA.CALCULATION.VIBRATION_AHV"]
        assert len(calc_records) >= 50
        for rec in calc_records[:10]:
            gt = rec.ground_truth.formula
            assert gt is not None
            assert gt.calculation_output is not None
            assert gt.valid is True

    def test_no_fabricated_formulas(self, corpus):
        registry_ids = {f.formula_id for f in corpus.formulas}
        records = build_formula_eval_dataset(corpus)
        for rec in records:
            for rel in rec.ground_truth.relevance:
                if rel.formula_id:
                    assert rel.formula_id in registry_ids

    def test_clarification_cases(self, corpus):
        records = build_formula_eval_dataset(corpus)
        clarify = [r for r in records if r.requires_clarification]
        assert len(clarify) >= 10


class TestConversationalDataset:
    def test_session_count(self, corpus):
        records = build_conversational_eval_dataset(corpus)
        sessions = {r.session_id for r in records if r.session_id}
        assert len(sessions) >= 300

    def test_turn_count(self, corpus):
        records = build_conversational_eval_dataset(corpus)
        assert len(records) >= 1000

    def test_multi_turn_sessions(self, corpus):
        records = build_conversational_eval_dataset(corpus)
        by_session: dict[str, list] = {}
        for r in records:
            if r.session_id:
                by_session.setdefault(r.session_id, []).append(r)
        multi = [s for s, turns in by_session.items() if len(turns) >= 3]
        assert len(multi) >= 100


class TestFormulaRouting:
    def test_direct_calculation(self, classifier):
        q = "با ahw1=1.5,t1=3,ahw2=2.5,t2=5 ahv را محاسبه کن"
        r = classifier.classify(q, slots={"variables": {"ahw_1": 1.5, "t_1": 3.0, "ahw_2": 2.5, "t_2": 5.0}})
        assert r.intent == "FORMULA.CALCULATION.VIBRATION_AHV"

    def test_formula_lookup_by_id(self, classifier):
        r = classifier.classify("فرمول formula_240_01 چیست؟")
        assert r.intent == "FORMULA.LOOKUP.BY_ID"

    def test_formula_domain_lookup(self, classifier):
        r = classifier.classify("رابطه محاسبه ارتعاش در صفحه 240")
        assert r.intent == "FORMULA.LOOKUP.BY_DOMAIN"

    def test_formula_clarify_missing_inputs(self, classifier):
        r = classifier.classify("ahv را محاسبه کن")
        assert r.requires_clarification or r.intent.startswith("CLARIFY")

    def test_formula_interpretation(self, classifier):
        r = classifier.classify("تفسیر نتیجه ahv")
        assert r.intent == "FORMULA.INTERPRETATION"


class TestReranker:
    def test_noop_preserves_order(self):
        cands = [
            RankedCandidate("a", "text a", 0.9),
            RankedCandidate("b", "text b", 0.8),
        ]
        out = NoOpReranker().rerank("query", cands, top_k=2)
        assert [c.chunk_id for c in out] == ["a", "b"]

    def test_lexical_reranker_changes_order(self):
        cands = [
            RankedCandidate("a", "unrelated content", 0.95),
            RankedCandidate("b", "TWA یعنی میانگین وزنی زمانی", 0.80),
        ]
        out = LexicalReranker().rerank("TWA یعنی چی", cands, top_k=2)
        assert out[0].chunk_id == "b"

    def test_available_rerankers_includes_baseline(self):
        rerankers = get_available_rerankers()
        names = {r.name for r in rerankers}
        assert "baseline_no_rerank" in names
        assert "lexical_overlap" in names


class TestRetrievalMetrics:
    def test_recall_at_k(self):
        assert recall_at_k(["a", "b", "c"], {"b"}, 3) == 1.0
        assert recall_at_k(["a", "b", "c"], {"d"}, 3) == 0.0

    def test_mrr(self):
        assert mrr(["x", "y", "target"], {"target"}) == 1 / 3


class TestChunkQuality:
    def test_analyze_runs(self):
        report = analyze_chunk_quality()
        assert report.total_chunks >= 500

    def test_no_gold_mutation(self):
        path = PROJECT_ROOT / "gold" / "rag" / "semantic_text_production.jsonl"
        if not path.exists():
            pytest.skip("semantic corpus not present")
        before = path.read_bytes()
        analyze_chunk_quality()
        after = path.read_bytes()
        assert before == after


class TestContextInheritance:
    def test_stel_duration_not_hybrid(self, classifier):
        r = classifier.classify(
            "مواجهه ۱۵ دقیقه‌ای مجاز Acetone",
            slots={"chemical_name": "Acetone"},
        )
        assert r.intent == "STRUCTURED.OEL.STEL_LOOKUP"
