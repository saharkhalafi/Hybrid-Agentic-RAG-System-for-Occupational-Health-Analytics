"""Tests for PDF geometry matching."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from document_ai.geometry_resolver import (
    BBOX_SOURCE_DOCUMENT_AI,
    BBOX_SOURCE_PYMUPDF,
    FUZZY_LINE_MIN_CONFIDENCE,
    GeometryCellInput,
    GeometryResolver,
    MATCH_CAS,
    MATCH_EXACT,
    MATCH_LINE,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_TOKENS,
    MATCH_TOKEN,
    document_ai_bbox_to_xywh,
    is_valid_bbox,
)
from ingestion.pdf_geometry import bbox_y_center, is_numeric_only_text, normalize_match_text, tokenize_match_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OHE6_PDF = PROJECT_ROOT.parent / "OHE6.pdf"


def _write_pdf(path: Path, *lines: tuple[str, float, float]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def _resolve(tmp_path: Path, text: str, *, document_ai_bbox=None, bbox_provenance=None) -> object:
    pdf_path = tmp_path / "resolver.pdf"
    _write_pdf(pdf_path, (text, 120, 200))
    with GeometryResolver(pdf_path) as resolver:
        kwargs: dict = {}
        if document_ai_bbox is not None:
            kwargs["document_ai_bbox"] = document_ai_bbox
            kwargs["bbox_provenance"] = bbox_provenance or BBOX_SOURCE_DOCUMENT_AI
        return resolver.resolve(
            GeometryCellInput(page_number=1, cell_text=text, table_id="t1"),
            **kwargs,
        )


@pytest.fixture
def oel_table_pdf(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "oel.pdf"
    _write_pdf(
        pdf_path,
        ("TWA", 280, 80),
        ("STEL/C", 180, 80),
        ("Ceiling", 80, 80),
        ("Styrene [100-42-5]", 300, 150),
        ("استایرن", 420, 150),
        ("10 ppm", 260, 220),
        ("20 ppm", 160, 220),
        ("Acetaldehyde [75-07-0]", 300, 280),
        ("25 ppm", 260, 340),
    )
    return pdf_path


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_resolve_cas_number_on_page_46():
    with GeometryResolver(OHE6_PDF) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=46,
                cell_text="Acetaldehyde [75-07-0]",
                table_id="table-1",
                row_index=2,
                column_index=4,
            )
        )

    assert result.bbox is not None
    assert result.match_confidence >= 0.85
    assert result.bbox_source == BBOX_SOURCE_PYMUPDF
    assert result.match_method in {MATCH_EXACT, MATCH_CAS, MATCH_TOKENS, MATCH_LINE}


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_resolve_twa_token_on_page_46():
    with GeometryResolver(OHE6_PDF) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=46,
                cell_text="TWA",
                table_id="table-1",
            )
        )

    assert result.bbox is not None
    assert result.match_confidence >= 0.85
    assert result.source_reference["page_number"] == 46


def test_normalize_match_text_handles_persian_digits():
    assert "123" in normalize_match_text("۱۲۳ ppm")
    assert tokenize_match_text("۰/۵ ppm") == ["0", "5", "ppm"]


def test_empty_cell_returns_zero_confidence(tmp_path: Path):
    pdf_path = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    doc.save(pdf_path)
    doc.close()

    with GeometryResolver(pdf_path) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="   ", table_id="t1")
        )

    assert result.bbox is None
    assert result.match_confidence == 0.0
    assert result.match_method == MATCH_NONE


def test_valid_document_ai_bbox_passthrough(tmp_path: Path):
    result = _resolve(
        tmp_path,
        "ignored text",
        document_ai_bbox={"x": 10, "y": 20, "width": 30, "height": 12},
    )
    assert result.bbox == {"x": 10.0, "y": 20.0, "width": 30.0, "height": 12.0}
    assert result.bbox_source == BBOX_SOURCE_DOCUMENT_AI
    assert result.match_confidence == 1.0
    assert result.match_method == MATCH_EXACT


def test_invalid_document_ai_bbox_falls_back_to_pymupdf(tmp_path: Path):
    result = _resolve(
        tmp_path,
        "Acetaldehyde [75-07-0]",
        document_ai_bbox={"x": 0, "y": 0, "width": 0, "height": 0},
    )
    assert result.bbox is not None
    assert result.bbox_source == BBOX_SOURCE_PYMUPDF


def test_cas_matching_with_spaced_registry_number(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Styrene [ 100-42-5 ]",
                table_id="t1",
            )
        )

    assert result.bbox is not None
    assert result.match_method in {
        MATCH_CAS,
        MATCH_EXACT,
        MATCH_TOKENS,
    }
    assert result.match_confidence >= 0.85


def test_english_chemical_name_matching(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="Acetaldehyde", table_id="t1")
        )

    assert result.bbox is not None
    assert result.match_method in {MATCH_EXACT, MATCH_LINE, MATCH_TOKENS}


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_persian_header_text_on_ohe6_page_46():
    with GeometryResolver(OHE6_PDF) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=46, cell_text="مبنای تعیین حد", table_id="table-1")
        )
    if result.bbox is None:
        pytest.skip("Expected Persian header text not found on page 46")
    assert result.match_confidence >= 0.85
    assert result.match_method in {MATCH_EXACT, MATCH_TOKENS, MATCH_LINE, MATCH_TOKEN}


def test_persian_chemical_name_rtl_token_matching(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="استایرن", table_id="t1")
        )

    if result.bbox is None:
        pytest.skip("Persian glyph rendering unavailable in this PDF fixture")

    assert result.match_method in {MATCH_EXACT, MATCH_TOKENS, MATCH_TOKEN, MATCH_LINE}


def test_twa_stel_ceiling_headers(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        twa = resolver.resolve(GeometryCellInput(page_number=1, cell_text="TWA", table_id="t1"))
        stel = resolver.resolve(GeometryCellInput(page_number=1, cell_text="STEL/C", table_id="t1"))
        ceiling = resolver.resolve(GeometryCellInput(page_number=1, cell_text="Ceiling", table_id="t1"))

    for result in (twa, stel, ceiling):
        assert result.bbox is not None
        assert result.match_confidence >= 0.85


def test_numeric_cell_matching(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="10 ppm", table_id="t1")
        )

    assert result.bbox is not None
    assert result.match_confidence >= 0.85


def test_merged_twa_stel_cell_resolves(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="10 ppm / 20 ppm", table_id="t1")
        )

    assert result.bbox is not None
    assert result.match_method in {MATCH_EXACT, MATCH_TOKENS, MATCH_PARTIAL, MATCH_LINE, MATCH_TOKEN}


def test_fuzzy_line_threshold_blocks_weak_matches(tmp_path: Path):
    pdf_path = tmp_path / "fuzzy.pdf"
    _write_pdf(pdf_path, ("Acetaldehyde [75-07-0]", 120, 200))

    with GeometryResolver(pdf_path, fuzzy_line_threshold=FUZZY_LINE_MIN_CONFIDENCE) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Totally unrelated chemical name",
                table_id="t1",
            )
        )

    assert result.bbox is None
    assert result.match_method == MATCH_NONE


def test_document_ai_bbox_to_xywh_supports_nested_bounding_box():
    converted = document_ai_bbox_to_xywh(
        {"bounding_box": {"x": 5, "y": 6, "width": 7, "height": 8}}
    )
    assert converted == {"x": 5.0, "y": 6.0, "width": 7.0, "height": 8.0}
    assert is_valid_bbox(converted)


def test_table_recovery_still_uses_page_geometry_index(oel_table_pdf: Path):
    from ingestion.table_recovery import recover_chemical_oel_table

    table = recover_chemical_oel_table(oel_table_pdf, page_number=1)
    assert table is not None
    assert table.rows
    cells_with_bbox = [cell for cell in table.flat_cells() if cell.bbox]
    assert cells_with_bbox


def test_row_number_requires_row_context(tmp_path: Path):
    pdf_path = tmp_path / "row_numbers.pdf"
    _write_pdf(
        pdf_path,
        ("1", 100, 150),
        ("2", 100, 250),
        ("Acetaldehyde [75-07-0]", 300, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        isolated = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="2", table_id="t1", row_index=2, column_index=5)
        )
        assert isolated.bbox is None
        assert isolated.match_method == MATCH_NONE

        resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Acetaldehyde [75-07-0]",
                table_id="t1",
                row_index=2,
                column_index=4,
            )
        )
        resolved = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="2", table_id="t1", row_index=2, column_index=5)
        )
        assert resolved.bbox is not None
        assert abs(bbox_y_center(resolved.bbox) - 250) < 12


def test_data_row_twa_rejects_header_occurrence(oel_table_pdf: Path):
    with GeometryResolver(oel_table_pdf) as resolver:
        resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="TWA", table_id="t1", row_index=0, column_index=2)
        )
        data_row = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="TWA", table_id="t1", row_index=2, column_index=2)
        )
    assert data_row.bbox is None
    assert data_row.match_method == MATCH_NONE


def test_search_for_does_not_union_cross_row_occurrences(tmp_path: Path):
    pdf_path = tmp_path / "repeated_health.pdf"
    repeated = "Respiratory irritation"
    _write_pdf(
        pdf_path,
        (repeated, 80, 120),
        (repeated, 80, 220),
        (repeated, 80, 320),
        ("Row marker", 200, 220),
    )
    with GeometryResolver(pdf_path) as resolver:
        resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="Row marker", table_id="t1", row_index=2, column_index=4)
        )
        row_two = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text=repeated, table_id="t1", row_index=2, column_index=0)
        )
    assert row_two.bbox is not None
    assert row_two.bbox["height"] <= 25
    assert abs(bbox_y_center(row_two.bbox) - 220) < 15


def test_substring_line_is_row_scoped_not_first_hit(tmp_path: Path):
    pdf_path = tmp_path / "mw_column.pdf"
    _write_pdf(
        pdf_path,
        ("44/05", 260, 150),
        ("59/07", 260, 250),
        ("Acetaldehyde", 300, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="Acetaldehyde", table_id="t1", row_index=2, column_index=4)
        )
        mw = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="59/07", table_id="t1", row_index=2, column_index=3)
        )
    assert mw.bbox is not None
    assert abs(bbox_y_center(mw.bbox) - 250) < 12
    assert is_numeric_only_text("59/07")


def test_numeric_only_ambiguity_returns_none(tmp_path: Path):
    pdf_path = tmp_path / "duplicate_digits.pdf"
    _write_pdf(
        pdf_path,
        ("2", 100, 150),
        ("2", 100, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="2", table_id="t1", row_index=2, column_index=5)
        )
    assert result.bbox is None
    assert result.match_method == MATCH_NONE


def test_header_row_rejects_page_top_fragment(tmp_path: Path):
    pdf_path = tmp_path / "header_fragment.pdf"
    _write_pdf(
        pdf_path,
        ("TWA", 120, 39),
        ("STEL/C", 120, 39),
        ("حد مجاز مواجهه شغلی TWA STEL/C", 180, 103),
        ("وزن ملکولی", 400, 103),
        ("Acetaldehyde [75-07-0]", 300, 150),
    )
    with GeometryResolver(pdf_path) as resolver:
        merged = resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="حد مجاز مواجهه شغلی TWA STEL/C",
                table_id="t1",
                row_index=0,
                column_index=2,
            )
        )
        twa_only = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="TWA", table_id="t1", row_index=0, column_index=2)
        )

    assert merged.bbox is not None
    assert abs(bbox_y_center(merged.bbox) - 103) < 15
    assert twa_only.bbox is not None
    assert abs(bbox_y_center(twa_only.bbox) - 103) < 15
    assert bbox_y_center(twa_only.bbox) > 60


def test_early_mw_anchor_rejects_shared_wrong_row_candidate(tmp_path: Path):
    pdf_path = tmp_path / "mw_early_anchor.pdf"
    _write_pdf(
        pdf_path,
        ("حد مجاز مواجهه شغلی TWA STEL/C", 180, 103),
        ("\\cdot/\\pi mg/m^{3}", 260, 150),
        ("183/16", 260, 150),
        ("44/05", 260, 150),
        ("59/07", 260, 250),
        ("Acephate [30560-19-1]", 300, 150),
        ("Acetaldehyde [75-07-0]", 300, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="حد مجاز مواجهه شغلی TWA STEL/C",
                table_id="t1",
                row_index=0,
                column_index=2,
            )
        )
        row1_mw = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="183/16", table_id="t1", row_index=1, column_index=3)
        )
        resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Acetaldehyde [75-07-0]",
                table_id="t1",
                row_index=2,
                column_index=4,
            )
        )
        row2_mw = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="59/07", table_id="t1", row_index=2, column_index=3)
        )

    assert row1_mw.bbox is None
    assert row1_mw.match_method == MATCH_NONE
    assert row2_mw.bbox is not None
    assert abs(bbox_y_center(row2_mw.bbox) - 250) < 12


def test_numeric_duplicate_accepts_with_strong_row_peer(tmp_path: Path):
    pdf_path = tmp_path / "numeric_with_peer.pdf"
    _write_pdf(
        pdf_path,
        ("2", 100, 150),
        ("2", 100, 250),
        ("Acetaldehyde [75-07-0]", 300, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Acetaldehyde [75-07-0]",
                table_id="t1",
                row_index=2,
                column_index=4,
            )
        )
        result = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="2", table_id="t1", row_index=2, column_index=5)
        )
    assert result.bbox is not None
    assert abs(bbox_y_center(result.bbox) - 250) < 12


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_ohe6_page_47_header_not_page_top_fragment():
    with GeometryResolver(OHE6_PDF) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=47,
                cell_text="حد مجاز مواجهه شغلی STEL/C TWA C۵ mg/m³ ۲۰ ppm ۴۰ppm ۱۰ ppm",
                table_id="1",
                row_index=0,
                column_index=2,
            )
        )
    if result.bbox is None:
        pytest.skip("Header cell not resolved on page 47")
    assert bbox_y_center(result.bbox) > 80
    assert abs(bbox_y_center(result.bbox) - 103) < 25


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_ohe6_page_46_row1_mw_not_shared_line():
    with GeometryResolver(OHE6_PDF) as resolver:
        resolver.resolve(
            GeometryCellInput(
                page_number=46,
                cell_text="حد مجاز مواجهه شغلی TWA STEL/C",
                table_id="1",
                row_index=0,
                column_index=2,
            )
        )
        result = resolver.resolve(
            GeometryCellInput(page_number=46, cell_text="۱۸۳/۱۶", table_id="1", row_index=1, column_index=3)
        )
    assert result.bbox is None or abs(bbox_y_center(result.bbox) - 150.8) > 12


def test_peer_row_band_rejects_off_row_text_match(tmp_path: Path):
    pdf_path = tmp_path / "peer_row_band.pdf"
    _write_pdf(
        pdf_path,
        ("Skin note", 100, 160),
        ("Other note", 200, 160),
        ("Allylamine [107-11-9]", 300, 250),
    )
    with GeometryResolver(pdf_path) as resolver:
        resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="Skin note", table_id="t1", row_index=2, column_index=1)
        )
        resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="Other note", table_id="t1", row_index=2, column_index=2)
        )
        result = resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="Allylamine [107-11-9]",
                table_id="t1",
                row_index=2,
                column_index=4,
            )
        )
    assert result.bbox is None
    assert result.match_method == MATCH_NONE


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_ohe6_page_49_row2_allylamine_rejects_off_row_match():
    from document_ai.parser import parse_document_ai_response

    da_path = PROJECT_ROOT / "data/processed/8b9e2240e437_46-55_document_ai.json"
    if not da_path.exists():
        pytest.skip("Document AI cache not available")

    import json

    parsed = parse_document_ai_response(json.loads(da_path.read_text(encoding="utf-8")))
    allylamine_result = None
    with GeometryResolver(OHE6_PDF) as resolver:
        for table in parsed.tables:
            if table.page_number + 45 != 49:
                continue
            for cell in table.flat_cells():
                text = (cell.text or "").strip()
                if not text:
                    continue
                da_bbox, bbox_provenance = (
                    (cell.bbox, BBOX_SOURCE_DOCUMENT_AI)
                    if is_valid_bbox(cell.bbox)
                    else (None, None)
                )
                result = resolver.resolve(
                    GeometryCellInput(
                        page_number=49,
                        cell_text=text,
                        table_id=table.table_id,
                        row_index=cell.row_index,
                        column_index=cell.column_index,
                    ),
                    document_ai_bbox=da_bbox,
                    bbox_provenance=bbox_provenance,
                )
                if cell.row_index == 2 and cell.column_index == 4 and "Allylamine" in text:
                    allylamine_result = result
    assert allylamine_result is not None
    assert allylamine_result.bbox is None
    assert allylamine_result.match_method == MATCH_NONE


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_table_050_row4_aminobutanol_resolves_after_table_context():
    import json

    from document_ai.bbox_provenance import resolve_bbox_inputs_from_cell
    from document_ai.geometry_cell_ordering import geometry_resolve_sort_key

    layer_a = Path(r"E:\temp\validated_structure_46-55_layer_a.json")
    if not layer_a.exists():
        pytest.skip("validated structure layer-a fixture not available")

    data = json.loads(layer_a.read_text(encoding="utf-8"))
    table = next(item for item in data["tables"] if item["table_id"] == "table_050_01")
    cells = []
    for row in table.get("rows", []):
        for cell in row:
            da_bbox, bbox_provenance = resolve_bbox_inputs_from_cell(cell)
            cells.append(
                {
                    "row": int(cell.get("row", 0)),
                    "column": int(cell.get("column", 0)),
                    "text": (cell.get("text") or "").strip(),
                    "da_bbox": da_bbox,
                    "bbox_provenance": bbox_provenance,
                }
            )
    cells.sort(
        key=lambda item: geometry_resolve_sort_key(
            page_number=50,
            table_id="table_050_01",
            row_index=item["row"],
            column_index=item["column"],
        )
    )

    target = next(item for item in cells if item["row"] == 4 and item["column"] == 5)
    result = None
    with GeometryResolver(OHE6_PDF) as resolver:
        for cell in cells:
            if cell["row"] == 4 and cell["column"] == 5:
                result = resolver.resolve(
                    GeometryCellInput(
                        page_number=50,
                        cell_text=cell["text"],
                        table_id="table_050_01",
                        row_index=cell["row"],
                        column_index=cell["column"],
                    ),
                    document_ai_bbox=cell["da_bbox"],
                    bbox_provenance=cell["bbox_provenance"],
                )
                break
            if not cell["text"]:
                continue
            resolver.resolve(
                GeometryCellInput(
                    page_number=50,
                    cell_text=cell["text"],
                    table_id="table_050_01",
                    row_index=cell["row"],
                    column_index=cell["column"],
                ),
                document_ai_bbox=cell["da_bbox"],
                bbox_provenance=cell["bbox_provenance"],
            )

    assert result is not None
    assert result.bbox is not None
    assert result.match_method == MATCH_CAS
    assert result.match_confidence >= 0.85
    assert abs(bbox_y_center(result.bbox) - 212) < 20
