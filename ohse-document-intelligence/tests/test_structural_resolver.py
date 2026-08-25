"""Layer 2 structural resolver — deterministic invariant tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from document_ai.bbox_provenance import (
    BBOX_PROVENANCE_DOCUMENT_AI,
    BBOX_PROVENANCE_PYMUPDF_ALIGNED,
)
from goldset_generator.document_processor import (
    ExtractedCellRecord,
    ExtractedTableRecord,
    PageTextRecord,
    ProcessedDocument,
)
from goldset_generator.structural_resolver import (
    EXPECTED_OEL_PHYSICAL_COLUMNS,
    StructuralResolverResult,
    _align_evidence_cells,
    _document_ai_table_quality_poor,
    _expand_oel_basis_symbols_column,
    _expand_oel_limit_columns,
    _needs_pymupdf_recovery,
    _oel_schema_signals,
    _reconstruct_oel_logical_rows,
    resolve_structure,
    write_validated_structure,
)

NUMERIC_TOKEN = re.compile(
    r"[\d۰-۹٠-٩]+(?:[./][\d۰-۹٠-٩]+)?",
)


def _cell(
    text: str,
    row: int,
    col: int,
    *,
    table_id: str = "table_t_01",
    page: int = 46,
    bbox: dict | None = None,
    bbox_source: str | None = None,
    bbox_confidence: float | None = None,
    source: str = "document_ai",
    source_reference: dict | None = None,
) -> ExtractedCellRecord:
    return ExtractedCellRecord(
        cell_id=f"cell_{table_id}_{row}_{col}",
        table_id=table_id,
        page_number=page,
        row=row,
        column=col,
        text=text,
        bbox=bbox,
        confidence=0.9,
        bbox_confidence=bbox_confidence,
        bbox_source=bbox_source,
        source=source,
        source_reference=source_reference or {},
    )


def _oel_table(
    rows: list[list[ExtractedCellRecord]],
    *,
    page: int = 46,
    table_id: str = "table_046_01",
) -> ExtractedTableRecord:
    return ExtractedTableRecord(
        table_id=table_id,
        page_number=page,
        table_type="chemical_oel",
        rows=rows,
        structural_confidence=0.9,
        raw_markdown="",
    )


def _six_col_oel_header(
    *,
    table_id: str = "table_046_01",
    page: int = 46,
) -> list[ExtractedCellRecord]:
    return [
        _cell("مبنای تعیین حد مجاز مواجهه", 0, 0, table_id=table_id, page=page),
        _cell("نمادها", 0, 1, table_id=table_id, page=page),
        _cell("حد مجاز مواجهه شغلی TWA STEL/C", 0, 2, table_id=table_id, page=page),
        _cell("وزن ملکولی", 0, 3, table_id=table_id, page=page),
        _cell("نام علمی ماده شیمیایی", 0, 4, table_id=table_id, page=page),
        _cell("ردیف", 0, 5, table_id=table_id, page=page),
    ]


def _seven_col_oel_header(
    *,
    table_id: str = "table_046_01",
    page: int = 46,
) -> list[ExtractedCellRecord]:
    return [
        _cell("مبنای تعیین حد مجاز مواجهه", 0, 0, table_id=table_id, page=page),
        _cell("نمادها", 0, 1, table_id=table_id, page=page),
        _cell("حد مجاز مواجهه شغلی STEL/C", 0, 2, table_id=table_id, page=page),
        _cell("TWA", 0, 3, table_id=table_id, page=page),
        _cell("وزن ملکولی", 0, 4, table_id=table_id, page=page),
        _cell("نام علمی ماده شیمیایی", 0, 5, table_id=table_id, page=page),
        _cell("ردیف", 0, 6, table_id=table_id, page=page),
    ]


def _numeric_tokens(text: str) -> list[str]:
    return NUMERIC_TOKEN.findall(text or "")


def _assert_table_consistency(table: ExtractedTableRecord) -> None:
    flat = table.flat_cells()
    ids = [cell.cell_id for cell in flat]
    assert len(ids) == len(set(ids)), "duplicate cell IDs"
    for row_index, row in enumerate(table.rows):
        for cell in row:
            assert cell.row == row_index
            assert cell.table_id == table.table_id
    for cell in flat:
        expected_id = f"cell_{cell.table_id}_{cell.row}_{cell.column}"
        assert cell.cell_id == expected_id


# ---------------------------------------------------------------------------
# 1. Valid 7-column OEL schema
# ---------------------------------------------------------------------------


def test_oel_seven_column_valid_schema_disjoint_limits():
    table = _oel_table([_seven_col_oel_header()])
    signals = _oel_schema_signals(table)
    assert signals["physical_column_count"] == 7
    assert signals["has_twa_header"]
    assert signals["has_stel_c_header"]
    assert signals["twa_and_stel_are_distinct_columns"]
    assert set(signals["twa_physical_columns"]).isdisjoint(
        set(signals["stel_c_physical_columns"]),
    )


# ---------------------------------------------------------------------------
# 2–5. Limit column expansion
# ---------------------------------------------------------------------------


def test_oel_six_to_seven_twa_stel_expansion():
    table = _oel_table(
        [
            _six_col_oel_header(),
            [
                _cell("effect", 1, 0, table_id="table_046_01"),
                _cell("A2", 1, 1, table_id="table_046_01"),
                _cell("۱۵ ppm ۱۰ ppm", 1, 2, table_id="table_046_01"),
                _cell("۶۰/۰۵", 1, 3, table_id="table_046_01"),
                _cell("Acetic acid [64-19-7]", 1, 4, table_id="table_046_01"),
                _cell("۵", 1, 5, table_id="table_046_01"),
            ],
        ],
    )
    expanded, count = _expand_oel_limit_columns(table)
    assert count == 1
    assert max(len(row) for row in expanded.rows) == 7
    assert expanded.rows[1][2].text == "۱۵ ppm"
    assert expanded.rows[1][3].text == "۱۰ ppm"
    _assert_table_consistency(expanded)


def test_oel_single_limit_value_goes_to_twa():
    table = _oel_table(
        [
            _six_col_oel_header(),
            [
                _cell("", 1, 0),
                _cell("", 1, 1),
                _cell("1 ppm(IFV)", 1, 2),
                _cell("44/05", 1, 3),
                _cell("Acetaldehyde [75-07-0]", 1, 4),
                _cell("2", 1, 5),
            ],
        ],
    )
    expanded, _ = _expand_oel_limit_columns(table)
    assert expanded.rows[1][2].text == ""
    assert expanded.rows[1][3].text == "1 ppm"


def test_oel_ceiling_value_goes_to_stel_column():
    table = _oel_table(
        [
            _six_col_oel_header(),
            [
                _cell("", 1, 0),
                _cell("A2", 1, 1),
                _cell("C 25 ppm", 1, 2),
                _cell("44/05", 1, 3),
                _cell("Acetaldehyde [75-07-0]", 1, 4),
                _cell("2", 1, 5),
            ],
        ],
    )
    expanded, _ = _expand_oel_limit_columns(table)
    assert "25" in expanded.rows[1][2].text
    assert expanded.rows[1][3].text == ""


def test_oel_limit_expansion_preserves_numeric_tokens():
    original = "۱۵ ppm ۱۰ ppm"
    table = _oel_table(
        [
            _six_col_oel_header(),
            [
                _cell("", 1, 0),
                _cell("", 1, 1),
                _cell(original, 1, 2),
                _cell("60/05", 1, 3),
                _cell("Acetic acid [64-19-7]", 1, 4),
                _cell("5", 1, 5),
            ],
        ],
    )
    expanded, _ = _expand_oel_limit_columns(table)
    merged_tokens = _numeric_tokens(original)
    final_tokens = _numeric_tokens(
        expanded.rows[1][2].text + " " + expanded.rows[1][3].text,
    )
    assert merged_tokens == final_tokens


# ---------------------------------------------------------------------------
# 6–7. Basis + symbols expansion
# ---------------------------------------------------------------------------


def test_oel_basis_symbols_expansion_header_and_data():
    table = _oel_table(
        [
            [
                _cell("مبنای تعیین حد نمادها مجاز مواجهه", 0, 0, table_id="table_051_01", page=51),
                _cell("حد مجاز مواجهه شغلی STEL/C", 0, 1, table_id="table_051_01", page=51),
                _cell("TWA", 0, 2, table_id="table_051_01", page=51),
                _cell("وزن ملکولی", 0, 3, table_id="table_051_01", page=51),
                _cell("نام علمی ماده شیمیایی", 0, 4, table_id="table_051_01", page=51),
                _cell("ردیف", 0, 5, table_id="table_051_01", page=51),
            ],
            [
                _cell("اثرات تیروئیدی A3", 1, 0, table_id="table_051_01", page=51),
                _cell("", 1, 1, table_id="table_051_01", page=51),
                _cell("0.5 ppm", 1, 2, table_id="table_051_01", page=51),
                _cell("94/12", 1, 3, table_id="table_051_01", page=51),
                _cell("2-Aminopyridine [504-29-0]", 1, 4, table_id="table_051_01", page=51),
                _cell("40", 1, 5, table_id="table_051_01", page=51),
            ],
        ],
        page=51,
        table_id="table_051_01",
    )
    expanded, count = _expand_oel_basis_symbols_column(table)
    assert count == 1
    assert max(len(row) for row in expanded.rows) == 7
    assert expanded.rows[0][0].text == "مبنای تعیین حد مجاز مواجهه"
    assert expanded.rows[0][1].text == "نمادها"
    assert expanded.rows[1][0].text == "اثرات تیروئیدی"
    assert expanded.rows[1][1].text == "A3"
    assert expanded.rows[1][3].text == "0.5 ppm"
    _assert_table_consistency(expanded)


def test_oel_basis_symbols_row_without_symbol():
    table = _oel_table(
        [
            [
                _cell("مبنای تعیین حد نمادها مجاز مواجهه", 0, 0),
                _cell("STEL/C", 0, 1),
                _cell("TWA", 0, 2),
                _cell("وزن ملکولی", 0, 3),
                _cell("نام علمی", 0, 4),
                _cell("ردیف", 0, 5),
            ],
            [
                _cell("basis text only", 1, 0),
                _cell("", 1, 1),
                _cell("", 1, 2),
                _cell("", 1, 3),
                _cell("Chem [11-11-1]", 1, 4),
                _cell("1", 1, 5),
            ],
        ],
        page=51,
    )
    expanded, _ = _expand_oel_basis_symbols_column(table)
    assert expanded.rows[1][0].text == "basis text only"
    assert expanded.rows[1][1].text == ""


# ---------------------------------------------------------------------------
# 8. Logical row reconstruction
# ---------------------------------------------------------------------------


def test_oel_logical_row_reconstruction_merges_continuation():
    table = _oel_table(
        [
            _six_col_oel_header(),
            [
                _cell("health fragment", 1, 0, table_id="table_046_01"),
                _cell("", 1, 1, table_id="table_046_01"),
                _cell("", 1, 2, table_id="table_046_01"),
                _cell("", 1, 3, table_id="table_046_01"),
                _cell("", 1, 4, table_id="table_046_01"),
                _cell("", 1, 5, table_id="table_046_01"),
            ],
            [
                _cell("", 2, 0, table_id="table_046_01"),
                _cell("A4", 2, 1, table_id="table_046_01"),
                _cell("0.05 mg/m3", 2, 2, table_id="table_046_01"),
                _cell("222.68", 2, 3, table_id="table_046_01"),
                _cell("Acetamiprid [135410-20-7]", 2, 4, table_id="table_046_01"),
                _cell("4", 2, 5, table_id="table_046_01"),
            ],
        ],
    )
    reconstructed, count = _reconstruct_oel_logical_rows(table)
    assert count >= 1
    assert any(
        "Acetamiprid" in (cell.text or "")
        for row in reconstructed.rows
        for cell in row
    )


# ---------------------------------------------------------------------------
# 9–10. Bbox provenance
# ---------------------------------------------------------------------------


def test_document_ai_bbox_not_overwritten_by_alignment(tmp_path: Path):
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
    bbox = {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
    cell = _cell(
        "25 ppm",
        1,
        2,
        bbox=bbox,
        bbox_source=BBOX_PROVENANCE_DOCUMENT_AI,
        bbox_confidence=0.95,
    )
    with patch(
        "goldset_generator.structural_resolver.resolve_cells_with_row_anchor_retry",
        return_value={cell.cell_id: type("G", (), {
            "resolved_bbox": {"x": 9.0, "y": 9.0, "width": 1.0, "height": 1.0},
            "bbox_confidence": 0.5,
            "bbox_source": BBOX_PROVENANCE_PYMUPDF_ALIGNED,
            "resolved_pass": 1,
        })()},
    ):
        aligned, count = _align_evidence_cells([cell], pdf)
    assert count == 0
    assert aligned[0].bbox == bbox
    assert aligned[0].bbox_source == BBOX_PROVENANCE_DOCUMENT_AI


def test_missing_bbox_can_receive_pymupdf_aligned(tmp_path: Path):
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
    cell = _cell("25 ppm", 1, 2, bbox=None, bbox_source=None)
    new_bbox = {"x": 5.0, "y": 6.0, "width": 7.0, "height": 8.0}
    with patch(
        "goldset_generator.structural_resolver.resolve_cells_with_row_anchor_retry",
        return_value={cell.cell_id: type("G", (), {
            "resolved_bbox": new_bbox,
            "bbox_confidence": 0.92,
            "bbox_source": BBOX_PROVENANCE_PYMUPDF_ALIGNED,
            "resolved_pass": 1,
        })()},
    ):
        aligned, count = _align_evidence_cells([cell], pdf)
    assert count == 1
    assert aligned[0].bbox == new_bbox
    assert aligned[0].bbox_source == BBOX_PROVENANCE_PYMUPDF_ALIGNED


# ---------------------------------------------------------------------------
# 11–14. Recovery decisions + non-OEL isolation
# ---------------------------------------------------------------------------


def test_recovery_not_triggered_for_good_seven_column_oel():
    page_text = "Acetaldehyde [75-07-0] Acetone [67-64-1]"
    table = _oel_table(
        [
            _seven_col_oel_header(),
            [
                _cell("", 1, 0),
                _cell("A4", 1, 1),
                _cell("", 1, 2),
                _cell("25 ppm", 1, 3),
                _cell("44/05", 1, 4),
                _cell("Acetaldehyde [75-07-0]", 1, 5),
                _cell("2", 1, 6),
            ],
        ],
    )
    assert _document_ai_table_quality_poor(page_text, table) is False
    assert _needs_pymupdf_recovery(page_text, [table]) is False


def test_recovery_triggered_for_header_cas_pollution():
    page_text = "Acrylamide [79-06-1] Acrylic acid [79-10-7] Acrylonitrile [107-13-1]"
    table = _oel_table(
        [
            [
                _cell("TWA", 0, 0),
                _cell("Acrylamide [79-06-1] Acrylic acid [79-10-7]", 0, 1),
            ],
            [_cell("", 1, 0), _cell("", 1, 1)],
        ],
        page=48,
    )
    assert _document_ai_table_quality_poor(page_text, table) is True
    assert _needs_pymupdf_recovery(page_text, [table]) is True


@pytest.mark.parametrize(
    ("table_type", "col_count"),
    [
        ("noise_limits", 4),
        ("noise_limits", 5),
        ("noise_limits", 6),
        ("noise_limits", 8),
    ],
)
def test_non_oel_tables_not_expanded(table_type: str, col_count: int):
    header = [_cell(f"h{i}", 0, i) for i in range(col_count)]
    table = ExtractedTableRecord(
        table_id="table_generic",
        page_number=240,
        table_type=table_type,
        rows=[header],
        structural_confidence=0.9,
        raw_markdown="",
    )
    expanded_limit, c1 = _expand_oel_limit_columns(table)
    expanded_basis, c2 = _expand_oel_basis_symbols_column(table)
    assert c1 == 0 and c2 == 0
    assert expanded_limit is table
    assert expanded_basis is table
    assert _needs_pymupdf_recovery("", [table]) is False


def test_recovery_failure_preserves_document_ai_tables(tmp_path: Path):
    page_text = "Acrylamide [79-06-1] Acrylic acid [79-10-7]"
    table = _oel_table(
        [
            [_cell("TWA", 0, 0), _cell("Acrylamide [79-06-1] Acrylic acid [79-10-7]", 0, 1)],
            [_cell("", 1, 0), _cell("", 1, 1)],
        ],
        page=48,
    )
    processed = ProcessedDocument(
        source_pdf="test.pdf",
        content_hash="abc",
        start_page=48,
        end_page=48,
        pages=[PageTextRecord(page_number=48, text=page_text, has_digital_text=True)],
        tables=[table],
        cells=[],
        raw_document_ai={},
        processor_format="layout_parser",
    )
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
    with patch(
        "goldset_generator.structural_resolver.recover_tables_for_page",
        return_value=[],
    ):
        result = resolve_structure(processed, pdf)
    assert any(t.table_id == table.table_id for t in result.tables)
    detection = result.page_detection[48]
    assert detection["document_ai_evidence_preserved"] is True
    assert detection["table_detection_status"] == "recovery_failed_document_ai_preserved"


# ---------------------------------------------------------------------------
# 18–21. Consistency + JSON output
# ---------------------------------------------------------------------------


def test_final_json_contains_oel_schema(tmp_path: Path):
    table = _oel_table([_seven_col_oel_header()])
    processed = ProcessedDocument(
        source_pdf="test.pdf",
        content_hash="abc",
        start_page=46,
        end_page=46,
        pages=[PageTextRecord(page_number=46, text="TWA STEL OEL", has_digital_text=True)],
        tables=[table],
        cells=[],
        raw_document_ai={},
        processor_format="layout_parser",
    )
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")
    result = resolve_structure(processed, pdf)
    path = write_validated_structure(result, processed, start_page=46, end_page=46)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pages_range"] == {"start": 46, "end": 46}
    assert "oel_schema" in payload["page_detection"]["46"]
    schema = payload["page_detection"]["46"]["oel_schema"]
    assert schema["detected"] is True
    signals = schema["tables"][0]["signals"]
    assert signals["expected_physical_column_count"] == EXPECTED_OEL_PHYSICAL_COLUMNS
    assert "column_count_status" in signals
    assert "schema_validity" in signals
    for table_payload in payload["tables"]:
        _assert_table_consistency(
            ExtractedTableRecord(
                table_id=table_payload["table_id"],
                page_number=table_payload["page_number"],
                table_type=table_payload["table_type"],
                rows=[
                    [
                        ExtractedCellRecord(
                            cell_id=c["cell_id"],
                            table_id=table_payload["table_id"],
                            page_number=c["page_number"],
                            row=ri,
                            column=c["column"],
                            text=c.get("text", ""),
                            bbox=c.get("bbox"),
                            confidence=c.get("confidence"),
                            bbox_confidence=c.get("bbox_confidence"),
                            bbox_source=c.get("bbox_source"),
                            source=c.get("source", "document_ai"),
                            normalized_value=c.get("normalized_value"),
                            source_reference=c.get("source_reference") or {},
                        )
                        for c in row
                    ]
                    for ri, row in enumerate(table_payload["rows"])
                ],
                structural_confidence=table_payload.get("structural_confidence"),
                raw_markdown=table_payload.get("raw_markdown", ""),
                bbox=table_payload.get("bbox"),
            ),
        )
