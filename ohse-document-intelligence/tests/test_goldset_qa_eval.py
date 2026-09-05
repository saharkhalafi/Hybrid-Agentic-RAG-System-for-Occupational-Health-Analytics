"""Unit tests for Goldset QA evaluator (mocked retrieval; no live 50-query run)."""

from __future__ import annotations

from agents.evaluation.goldset_qa_eval import GoldsetQAEvaluator
from agents.evaluation.goldset_qa_loader import GoldsetQAItem
from agents.evaluation.goldset_qa_relevance import (
    is_formula_record_relevant,
    is_semantic_chunk_relevant,
    is_structured_record_relevant,
)


def _gold(**overrides) -> GoldsetQAItem:
    base = dict(
        id="Q99",
        question="TWA benzene چیست؟",
        reference_answer="0.5 ppm",
        source_page=48,
        source_text="TWA بنزن برابر 0.5 ppm است",
        question_type="numeric",
    )
    base.update(overrides)
    return GoldsetQAItem(**base)


def test_semantic_page_and_text_hit():
    gold = _gold(
        id="Q01",
        question="تعریف مواجهه شغلی چیست؟",
        reference_answer="تعریف مواجهه",
        source_page=12,
        source_text="مواجهه شغلی عبارت است از تماس کارگر با عامل زیان آور",
        question_type="definition",
    )
    chunks = [
        {
            "chunk_id": "c1",
            "content": "مواجهه شغلی عبارت است از تماس کارگر با عامل زیان آور در محیط کار",
            "page_number": 12,
            "printed_page_number": 12,
            "score": 0.91,
        },
        {
            "chunk_id": "c2",
            "content": "متن نامرتبط",
            "page_number": 99,
            "printed_page_number": 99,
            "score": 0.2,
        },
    ]
    assert is_semantic_chunk_relevant(chunks[0], gold) is True
    evaluator = GoldsetQAEvaluator()
    result = evaluator.evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["semantic"],
            "semantic": {"success": True, "chunks": chunks},
        },
        ranked_semantic_chunks=chunks,
    )
    assert result.actual_agents == ["semantic"]
    assert result.semantic is not None
    assert result.structured is None
    assert result.semantic["hit@1"] == 1.0
    assert result.semantic["hit@3"] == 1.0
    assert result.semantic["mrr"] == 1.0
    assert result.semantic["recall@5"] == 1.0
    assert result.semantic_chunks[0]["printed_page_number"] == 12
    assert result.semantic_chunks[0]["rank"] == 1
    assert result.semantic_chunks[0]["score"] == 0.91


def test_wrong_page_is_not_relevant():
    gold = _gold(
        question_type="narrative",
        source_page=20,
        source_text="حد مجاز مواجهه بر اساس میانگین زمانی است",
        reference_answer="میانگین زمانی",
    )
    chunk = {
        "chunk_id": "c-wrong-page",
        "content": "حد مجاز مواجهه بر اساس میانگین زمانی است",
        "page_number": 21,
        "printed_page_number": 21,
    }
    assert is_semantic_chunk_relevant(chunk, gold) is False
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["semantic"],
            "semantic": {"success": True, "chunks": [chunk]},
        },
        ranked_semantic_chunks=[chunk],
    )
    assert result.semantic["hit@1"] == 0.0
    assert result.semantic["hit@10"] == 0.0
    assert result.semantic["mrr"] == 0.0


def test_structured_exact_match():
    gold = _gold(
        id="Q35",
        question="TWA benzene چقدر است؟",
        reference_answer="0.5",
        source_page=48,
        source_text="Benzene TWA 0.5 ppm",
        question_type="numeric",
    )
    record = {
        "success": True,
        "chemical_name": "Benzene",
        "cas": "71-43-2",
        "field": "twa",
        "value": 0.5,
        "page_number": 48,
        "source_row_key": "oel:48:benzene",
        "cell_id": "cell-twa-1",
        "score": 1.0,
    }
    assert is_structured_record_relevant(record, gold) is True
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["structured"],
            "structured": {
                "success": True,
                "data": record,
                "citations": [
                    {
                        "source_row_key": "oel:48:benzene",
                        "cell_id": "cell-twa-1",
                        "page_number": 48,
                    }
                ],
            },
        },
    )
    assert result.structured is not None
    assert result.semantic is None
    assert result.structured["hit@1"] == 1.0
    assert result.structured["precision@1"] == 1.0
    assert result.structured["recall@1"] == 1.0
    assert result.structured["mrr"] == 1.0
    assert result.structured_records[0]["source_row_key"] == "oel:48:benzene"
    assert result.structured_records[0]["cell_id"] == "cell-twa-1"
    assert result.structured_records[0]["rank"] == 1


def test_structured_wrong_chemical():
    gold = _gold(
        question="TWA benzene چقدر است؟",
        reference_answer="0.5",
        source_page=48,
        source_text="Benzene TWA 0.5 ppm",
        question_type="numeric",
    )
    record = {
        "success": True,
        "chemical_name": "Toluene",
        "cas": "108-88-3",
        "field": "twa",
        "value": 0.5,
        "page_number": 48,
        "source_row_key": "oel:48:toluene",
        "cell_id": "cell-wrong",
    }
    assert is_structured_record_relevant(record, gold) is False
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["structured"],
            "structured": {"success": True, "data": record, "citations": []},
        },
    )
    assert result.structured["hit@1"] == 0.0
    assert result.structured["mrr"] == 0.0
    assert result.diagnostics["actual_agents"] == ["structured"]


def test_numeric_answer_mismatch():
    gold = _gold(
        question="TWA benzene چقدر است؟",
        reference_answer="0.5 ppm",
        source_page=48,
        source_text="Benzene TWA 0.5 ppm",
        question_type="numeric",
    )
    record = {
        "success": True,
        "chemical_name": "Benzene",
        "cas": "71-43-2",
        "field": "twa",
        "value": 5.0,
        "page_number": 48,
        "source_row_key": "oel:48:benzene",
        "cell_id": "cell-twa-1",
    }
    assert is_structured_record_relevant(record, gold) is False
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["structured"],
            "structured": {"success": True, "data": record, "citations": []},
        },
    )
    assert result.structured["hit@1"] == 0.0
    assert result.structured["precision@1"] == 0.0
    assert result.structured["recall@1"] == 0.0


def test_formula_page_and_text_match():
    gold = _gold(
        id="Q20",
        question="فرمول IPM چیست؟",
        reference_answer="IPM = ...",
        source_page=168,
        source_text="IPM dae ذرات قابل استنشاق",
        question_type="formula",
    )
    record = {
        "success": True,
        "formula_id": "formula_ipm",
        "page_number": 168,
        "original_expression": "IPM = f(dae)",
        "normalized_expression": "IPM",
        "description": "ذرات قابل استنشاق",
    }
    assert is_formula_record_relevant(record, gold) is True
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["formula"],
            "formula": {
                "success": True,
                "data": record,
                "citations": [{"formula_id": "formula_ipm", "page_number": 168}],
            },
        },
    )
    assert result.formula is not None
    assert result.formula["hit@1"] == 1.0
    assert result.semantic is None
    assert result.structured is None
    assert result.formula_records[0]["formula_id"] == "formula_ipm"
    assert result.formula_records[0]["expression"] == "IPM = f(dae)"


def test_empty_no_result():
    gold = _gold(question_type="definition", source_page=3, source_text="تعریف حد مجاز")
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["semantic"],
            "semantic": {"success": False, "chunks": []},
        },
        ranked_semantic_chunks=[],
    )
    assert result.no_result is True
    assert result.semantic["hit@1"] == 0.0
    assert result.semantic["mrr"] == 0.0
    assert result.semantic["recall@10"] == 0.0
    assert result.semantic["precision@1"] == 0.0


def test_mixed_table_routing_keeps_metrics_separate():
    gold = _gold(
        id="Q31",
        question="TWA و اثرات بهداشتی benzene در جدول چیست؟",
        reference_answer="0.5 ppm",
        source_page=48,
        source_text="Benzene در جدول OEL با TWA 0.5",
        question_type="table",
    )
    structured = {
        "success": True,
        "chemical_name": "Benzene",
        "cas": "71-43-2",
        "field": "twa",
        "value": 0.5,
        "page_number": 48,
        "source_row_key": "oel:48:benzene",
        "cell_id": "cell-1",
    }
    chunks = [
        {
            "chunk_id": "tbl-narr",
            "content": "Benzene در جدول OEL با TWA 0.5 و اثرات بهداشتی ذکر شده است",
            "page_number": 48,
            "printed_page_number": 48,
        }
    ]
    result = GoldsetQAEvaluator().evaluate_case(
        gold,
        agent_results={
            "planned_agents": ["structured", "semantic"],
            "hybrid": {
                "success": True,
                "agent_results": {
                    "execution_plan": {"agents": ["structured", "semantic"]},
                    "structured": {
                        "success": True,
                        "data": structured,
                        "citations": [
                            {
                                "source_row_key": "oel:48:benzene",
                                "cell_id": "cell-1",
                                "page_number": 48,
                            }
                        ],
                    },
                    "semantic": {"success": True, "chunks": chunks},
                },
            },
        },
        ranked_semantic_chunks=chunks,
    )
    assert result.mixed_table is True
    assert result.metric_families == ["structured", "semantic"]
    assert result.structured is not None and result.semantic is not None
    assert result.structured["hit@1"] == 1.0
    assert result.semantic["hit@1"] == 1.0
    assert "precision@3" not in result.structured
    assert "precision@1" in result.structured
    assert "precision@5" in result.semantic
    assert result.diagnostics["actual_intent"] is None
    assert "structured" in result.actual_agents
    assert "semantic" in result.actual_agents
