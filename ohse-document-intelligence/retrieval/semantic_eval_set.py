"""Deterministic Persian semantic retrieval evaluation set (OHE6 Gold corpus).

Each query is grounded in content present in gold/rag/semantic_text_production.jsonl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SemanticEvalCase:
    query_id: str
    query: str
    intent: str
    expected_topic: str
    expected_chunk_ids: list[str]
    expected_pages: list[int] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "intent": self.intent,
            "expected_topic": self.expected_topic,
            "expected_chunk_ids": self.expected_chunk_ids,
            "expected_pages": self.expected_pages,
            "notes": self.notes,
        }


# Ground truth derived from semantic_text_production.jsonl section titles and chunk content.
SEMANTIC_EVAL_CASES: list[SemanticEvalCase] = [
    SemanticEvalCase(
        query_id="q01_definition_oel",
        query="حدود مجاز مواجهه شغلی (OEL) چیست و به چه چیزی اشاره دارد؟",
        intent="definition",
        expected_topic="تعریف حدود مجاز مواجهۀ شغلی",
        expected_chunk_ids=["semantic_023_01", "semantic_023_02"],
        expected_pages=[23],
    ),
    SemanticEvalCase(
        query_id="q02_intro_chemical",
        query="مقدمه فصل حدود مجاز مواجهه با عوامل شیمیایی چه هدفی دارد؟",
        intent="explanation",
        expected_topic="مقدمه — عوامل زیان‌آور شیمیایی",
        expected_chunk_ids=["semantic_021_01", "semantic_021_02"],
        expected_pages=[21],
    ),
    SemanticEvalCase(
        query_id="q03_twa_eight_hour",
        query="تعریف TWA و مواجهه 8 ساعت کار روزانه چیست؟",
        intent="occupational_exposure_concept",
        expected_topic="TWA — 8 ساعت کار",
        expected_chunk_ids=["semantic_025_01"],
        expected_pages=[25],
    ),
    SemanticEvalCase(
        query_id="q04_stel_short_term",
        query="حد STEL و مواجهه کوتاه مدت 15 دقیقه‌ای چگونه تعریف شده است؟",
        intent="occupational_exposure_concept",
        expected_topic="STEL — 15 دقیقه",
        expected_chunk_ids=["semantic_026_01", "semantic_025_02"],
        expected_pages=[25, 26],
    ),
    SemanticEvalCase(
        query_id="q05_ceiling_limit",
        query="حد سقف (Ceiling) مواجهه برای گازهای محرک چگونه بیان شده است؟",
        intent="regulatory_context",
        expected_topic="سقف مواجهه",
        expected_chunk_ids=["semantic_024_02"],
        expected_pages=[24],
    ),
    SemanticEvalCase(
        query_id="q06_individual_sensitivity",
        query="عوامل فردی مانند ژنتیک و سیگار چگونه بر حساسیت به مواجهه شیمیایی اثر می‌گذارند؟",
        intent="chemical_hazard_description",
        expected_topic="حساسیت فردی",
        expected_chunk_ids=["semantic_022_01", "semantic_024_01"],
        expected_pages=[22, 24],
    ),
    SemanticEvalCase(
        query_id="q07_exposure_assessment_basis",
        query="منابع و مبانی تعیین حد مجاز مواجهه شغلی در ویرایش ششم چیست؟",
        intent="exposure_assessment",
        expected_topic="منابع تعیین حدود",
        expected_chunk_ids=["semantic_022_02", "semantic_022_03"],
        expected_pages=[22],
    ),
    SemanticEvalCase(
        query_id="q08_biological_bei",
        query="شاخص بیولوژیکی مواجهه (BEI) و نماد BEIs چیست؟",
        intent="biological_exposure",
        expected_topic="BEI — شاخص بیولوژیک",
        expected_chunk_ids=["semantic_036_01"],
        expected_pages=[36],
    ),
    SemanticEvalCase(
        query_id="q09_noise_limits",
        query="حد مجاز مواجهه شغلی با صدا و فراصوت چگونه تعریف شده است؟",
        intent="noise",
        expected_topic="حد مجاز صدا",
        expected_chunk_ids=["semantic_221_01", "semantic_221_02"],
        expected_pages=[221],
    ),
    SemanticEvalCase(
        query_id="q10_vibration_physical",
        query="حدود مجاز مواجهه با عوامل فیزیکی شامل صدا و ارتعاش در این کتابچه چگونه معرفی شده‌اند؟",
        intent="vibration",
        expected_topic="عوامل فیزیکی — صدا و ارتعاش",
        expected_chunk_ids=["semantic_219_01"],
        expected_pages=[219],
    ),
]
