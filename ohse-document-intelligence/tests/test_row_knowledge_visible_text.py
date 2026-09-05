"""Row-knowledge retrieval text includes full visible cell/original values."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from persistence.canonical_chunk_store import format_oel_row_content
from persistence.evidence_pipeline import persist_semantic_and_row_chunks
from persistence.row_knowledge_text import (
    already_covered,
    format_row_knowledge_content,
    gold_row_knowledge_content,
    labeled_and_original_from_gold_field,
)


def test_original_cell_text_appended_when_display_is_compressed():
    content = gold_row_knowledge_content(
        {
            "chemical_name": {
                "value": "Acetonitrile",
                "original_value": "استونیتریل\n[75-05-8] Acetonitrile",
                "value_status": "extracted",
            },
            "TWA": {
                "value": "20",
                "unit": "ppm",
                "original_value": "20 ppm",
                "value_status": "extracted",
            },
        }
    )
    assert "chemical_name: Acetonitrile" in content
    assert "TWA: 20 ppm" in content
    assert "استونیتریل" in content
    assert "75-05-8" in content


def test_does_not_duplicate_original_when_already_in_display():
    labeled, extra = labeled_and_original_from_gold_field(
        "TWA",
        {"value": "20 ppm", "unit": "ppm", "original_value": "20 ppm"},
    )
    assert labeled == ["TWA: 20 ppm"]
    assert extra == []


def test_does_not_invent_missing_originals():
    content = gold_row_knowledge_content(
        {"TWA": {"value": "5", "unit": "ppm", "value_status": "extracted"}}
    )
    assert content == "TWA: 5 ppm"
    assert "STEL" not in content


def test_absent_field_without_original_is_omitted():
    content = gold_row_knowledge_content(
        {"CAS": {"value": None, "value_status": "absent"}}
    )
    assert content == ""


def test_format_skips_text_already_covered():
    text = format_row_knowledge_content(
        labeled_parts=["chemical_name: Indene"],
        extra_visible=["chemical_name: Indene"],
    )
    assert text == "chemical_name: Indene"
    assert already_covered("Indene", "chemical_name: Indene") is True


def test_oel_row_includes_persian_name_and_original_values():
    row = SimpleNamespace(
        persian_name="استونیتریل",
        english_name="Acetonitrile",
        twa=20.0,
        stel=None,
        ceiling=None,
        unit="ppm",
        page_number=47,
        source_row_key="table_047_01:row_2",
        original_values={
            "TWA": {"original_value": "20 ppm", "accepted_value": 20},
        },
    )
    content = format_oel_row_content(row, cas="75-05-8")
    assert "استونیتریل" in content
    assert "Acetonitrile" in content
    assert "75-05-8" in content
    assert "20 ppm" in content


def test_persist_row_chunks_keep_original_visible_text():
    session = MagicMock()
    document = SimpleNamespace(id="doc-1")
    persist_semantic_and_row_chunks(
        session,
        document,
        semantic_records=[],
        table_golds=[
            {
                "page_number": 47,
                "table_id": "table_047_01",
                "rows": [
                    {
                        "chemical_name": {
                            "value": "Acetonitrile",
                            "original_value": "استونیتریل [75-05-8] Acetonitrile",
                            "cell_id": "cell_table_047_01_2_5",
                            "bbox": None,
                            "value_status": "extracted",
                        }
                    }
                ],
            }
        ],
    )
    added = session.add.call_args_list
    assert added
    chunk = added[0].args[0]
    assert "Acetonitrile" in chunk.content
    assert "استونیتریل" in chunk.content
    assert "75-05-8" in chunk.content
    assert chunk.source_type == "row_knowledge"
