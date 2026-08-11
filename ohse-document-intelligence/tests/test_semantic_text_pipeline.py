"""Tests for semantic text preparation layer."""

from __future__ import annotations

from goldset_generator.page_structure_detector import detect_page_structure
from goldset_generator.persian_text_repair import (
    build_raw_text,
    build_repaired_text,
    repair_ocr_text,
)
from goldset_generator.semantic_text_generator import SemanticTextGenerator
from goldset_generator.semantic_text_validator import SemanticTextValidator


def test_ocr_repair_common_fragments():
    assert "تعیین شده" in repair_ocr_text("تع\nن\nیی\nشده")
    assert "می\u200cتواند" in repair_ocr_text("می\nن\nتوان\nد")
    assert "شیمیایی" in repair_ocr_text("مواد ش\nیمیایی")
    assert "به طور کلی" in repair_ocr_text("به طورکل\nی")
    assert "OELs" in repair_ocr_text("OELs برابر")


def test_raw_vs_repaired_text_layers():
    raw_a = "تع\nن\nیی\nشده"
    raw = build_raw_text([raw_a])
    repaired = build_repaired_text([raw_a])
    assert "تع" in raw
    assert "تعیین شده" in repaired
    assert raw != repaired


def test_structure_rejects_peyvast_sentence_fragment():
    paragraphs = [
        {
            "text": "پیوست این بخش منتقل شده اند. در ضمن در تدوین این حدود سعی شده است",
            "bbox": {"x": 70, "y": 400, "width": 350, "height": 15, "leading_indent": 6},
        }
    ]
    structure = detect_page_structure(paragraphs, page_number=22)
    assert all("منتقل شده" not in (s.title or "") for s in structure.sections)


def test_structure_rejects_sentence_fragment_heading():
    paragraphs = [
        {
            "text": "شرایطی را بیان می کند که اگر کارگران به طور مداوم با مواد شیمیایی مواجهه داشته باشند",
            "bbox": {"x": 70, "y": 100, "width": 350, "height": 15},
        }
    ]
    structure = detect_page_structure(paragraphs, page_number=23)
    assert all(s.title != paragraphs[0]["text"][:40] for s in structure.sections)


def test_structure_detects_real_headings():
    paragraphs = [
        {"text": "بخش اول: حدود مجاز مواجهۀ شغلی با عوامل شیمیایی", "bbox": {"x": 70, "y": 90, "width": 350, "height": 15}},
        {"text": "مقدمه", "bbox": {"x": 70, "y": 112, "width": 350, "height": 15}},
        {"text": "       منابع اصلی که در تعیین این حد مجاز مواجهه شغلی", "bbox": {"x": 70, "y": 400, "width": 350, "height": 15}},
    ]
    structure = detect_page_structure(paragraphs, page_number=22)
    assert 2 in structure.topic_boundaries


def test_chunk_splits_sources_section():
    generator = SemanticTextGenerator()
    paragraphs = [
        {"text": "عالوه بر حساسیت های فردی عوامل دیگری نیز می ن توان د در مواجهه با غلظت های برابر", "bbox": {"x": 70, "y": 90, "width": 350, "height": 15}},
        {"text": "استعمال دخانیات می تواند بدن را در مواجهه با مواد شیمیایی تضعیف کند.", "bbox": {"x": 70, "y": 110, "width": 350, "height": 15}},
        {"text": "       منابع اصلی که در تعیین این حد مجاز مواجهه شغلی مورد استفاده قرار گرفته", "bbox": {"x": 70, "y": 400, "width": 350, "height": 15}},
        {"text": "از اطلاعات حاصل از تجارب محیط کار مطالعات تجربی بر روی انسان است.", "bbox": {"x": 70, "y": 420, "width": 350, "height": 15}},
    ]
    chunks = generator.generate_for_page(
        page_number=22,
        printed_page_number=21,
        paragraphs=paragraphs,
        document_id="doc",
        source_pdf="OHE6.pdf",
    )
    assert len(chunks) >= 2
    assert "منابع" not in chunks[0]["normalized_text"]
    assert "منابع" in chunks[1]["normalized_text"]


def test_chunk_has_three_text_layers():
    generator = SemanticTextGenerator()
    paragraphs = [
        {
            "text": "در هنگام استفاده از OELs برای ارزیابی مخاطرات بهداشتی ناشی از مواجهه همزمان با مخلوطی از مواد، باید از مدل مناسب استفاده شود.",
            "bbox": {"x": 70, "y": 90, "width": 350, "height": 20},
        },
    ]
    chunks = generator.generate_for_page(
        page_number=29,
        printed_page_number=28,
        paragraphs=paragraphs,
        document_id="doc",
        source_pdf="OHE6.pdf",
    )
    chunk = chunks[0]
    assert chunk["raw_text"]
    assert chunk["normalized_text"]
    assert chunk["semantic_text"] is None
    assert chunk["semantic_enrichment"] is None
    assert chunk["provenance"]["llm_rewritten"] is False


def test_running_header_does_not_drop_body_paragraph():
    from goldset_generator.semantic_text_generator import _is_running_header

    assert not _is_running_header("که منجر به کاهش مواجهه شاغلین با عوامل شیمیایی")
    assert _is_running_header("حدود مجاز \n مواجهه شغلی-\n وزارت بهداشت ، درمان وآموزش پزشکی")


def test_semantic_validator_flags_table_leakage(tmp_path):
    validator = SemanticTextValidator(
        gold_dir=tmp_path,
        paragraph_evidence={"text_evidence_001_000": "sample"},
        table_cell_texts={"Acrylamide [79-06-1] 0.03 mg/m3"},
    )
    chunk = {
        "chunk_id": "semantic_001_01",
        "chunk_type": "semantic_text",
        "raw_text": "Acrylamide [79-06-1] 0.03 mg/m3",
        "text": "Acrylamide [79-06-1] 0.03 mg/m3",
        "normalized_text": "Acrylamide [79-06-1] 0.03 mg/m3",
        "semantic_text": None,
        "pdf_page_number": 1,
        "bbox": {"x": 1, "y": 1, "width": 10, "height": 10},
        "provenance": {"text_evidence_ids": ["text_evidence_001_000"], "llm_rewritten": False},
        "source_references": {
            "page_ids": ["page_001"],
            "text_evidence_ids": ["text_evidence_001_000"],
            "entity_ids": [],
            "table_ids": [],
            "formula_ids": [],
        },
        "retrieval_mode": "semantic",
        "embedding_status": "pending",
    }
    issues = validator.validate_chunk(chunk)
    assert any("table" in issue.lower() for issue in issues)
