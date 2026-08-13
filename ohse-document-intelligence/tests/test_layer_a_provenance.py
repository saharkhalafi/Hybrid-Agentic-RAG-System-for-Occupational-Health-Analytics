"""Layer-A regression tests: bbox provenance, visual row bands, LaTeX search normalization."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from document_ai.bbox_provenance import (
    BBOX_PROVENANCE_DOCUMENT_AI,
    BBOX_PROVENANCE_PYMUPDF_ALIGNED,
    is_trusted_document_ai_bbox,
    resolve_bbox_inputs,
    resolve_bbox_inputs_from_cell,
)
from document_ai.geometry_resolver import (
    BBOX_SOURCE_DOCUMENT_AI,
    BBOX_SOURCE_PYMUPDF,
    GeometryCellInput,
    GeometryResolver,
    MATCH_EXACT,
    MATCH_NONE,
    is_valid_bbox,
)
from goldset_generator.document_processor import ExtractedCellRecord
from goldset_generator.row_visual_band import (
    row_has_multi_visual_band,
    split_row_by_visual_bands,
)
from ingestion.pdf_geometry import normalize_match_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OHE6_PDF = PROJECT_ROOT.parent / "OHE6.pdf"


def _write_pdf(path: Path, *lines: tuple[str, float, float]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def test_pymupdf_recovery_bbox_not_trusted_document_ai():
    recovery_bbox = {"x": 10.0, "y": 129.2, "width": 40.0, "height": 10.0}
    assert not is_trusted_document_ai_bbox(recovery_bbox, BBOX_PROVENANCE_PYMUPDF_ALIGNED)
    da_bbox, provenance = resolve_bbox_inputs(recovery_bbox, BBOX_PROVENANCE_PYMUPDF_ALIGNED)
    assert da_bbox is None
    assert provenance is None


def test_genuine_document_ai_bbox_is_trusted():
    da_bbox = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 12.0}
    assert is_trusted_document_ai_bbox(da_bbox, BBOX_PROVENANCE_DOCUMENT_AI)
    trusted, provenance = resolve_bbox_inputs(da_bbox, BBOX_PROVENANCE_DOCUMENT_AI)
    assert trusted == da_bbox
    assert provenance == BBOX_PROVENANCE_DOCUMENT_AI


def test_recovery_bbox_does_not_passthrough_as_document_ai(tmp_path: Path):
    pdf_path = tmp_path / "recovery_hint.pdf"
    _write_pdf(
        pdf_path,
        ("Aluminum [7429-90-5]", 300, 129),
        ("0/8 f/ml", 180, 172),
    )
    recovery_bbox = {"x": 170.0, "y": 125.0, "width": 50.0, "height": 10.0}
    with GeometryResolver(pdf_path) as resolver:
        with_guard = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="0/8 f/ml", table_id="t1", row_index=3, column_index=2),
            document_ai_bbox=recovery_bbox,
            bbox_provenance=BBOX_PROVENANCE_PYMUPDF_ALIGNED,
        )
    with GeometryResolver(pdf_path) as resolver:
        without_guard = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="0/8 f/ml", table_id="t2", row_index=3, column_index=2),
            document_ai_bbox=recovery_bbox,
            bbox_provenance=BBOX_PROVENANCE_DOCUMENT_AI,
        )
    assert without_guard.bbox_source == BBOX_SOURCE_DOCUMENT_AI
    assert without_guard.bbox["y"] == pytest.approx(125.0)
    assert with_guard.bbox_source == BBOX_SOURCE_PYMUPDF
    assert with_guard.bbox is not None
    assert with_guard.bbox["y"] != pytest.approx(125.0, abs=2.0)


def test_page50_style_recovery_y129_cannot_poison_row_target(tmp_path: Path):
    pdf_path = tmp_path / "page50_band.pdf"
    _write_pdf(
        pdf_path,
        ("limit A", 180, 129),
        ("limit B", 180, 229),
        ("Al compound", 300, 229),
    )
    poison_bbox = {"x": 170.0, "y": 124.0, "width": 60.0, "height": 12.0}
    with GeometryResolver(pdf_path) as resolver:
        result = resolver.resolve(
            GeometryCellInput(
                page_number=1,
                cell_text="limit B",
                table_id="t1",
                row_index=5,
                column_index=2,
            ),
            document_ai_bbox=poison_bbox,
            bbox_provenance=BBOX_PROVENANCE_PYMUPDF_ALIGNED,
        )
    assert result.bbox is not None
    assert result.bbox["y"] == pytest.approx(229.0, abs=15.0)
    assert result.bbox["y"] > 180.0
    assert result.bbox_source == BBOX_SOURCE_PYMUPDF


def test_multi_visual_band_row_is_detected_and_split():
    row = [
        ExtractedCellRecord(
            cell_id="c0",
            table_id="t1",
            page_number=50,
            row=3,
            column=2,
            text="wrong limit",
            bbox={"x": 1, "y": 124, "width": 10, "height": 10},
            confidence=None,
            bbox_confidence=0.9,
            bbox_source=BBOX_PROVENANCE_PYMUPDF_ALIGNED,
            source="document_ai",
        ),
        ExtractedCellRecord(
            cell_id="c1",
            table_id="t1",
            page_number=50,
            row=3,
            column=5,
            text="Al compound",
            bbox={"x": 1, "y": 171, "width": 10, "height": 10},
            confidence=None,
            bbox_confidence=0.9,
            bbox_source=BBOX_PROVENANCE_PYMUPDF_ALIGNED,
            source="document_ai",
        ),
    ]
    assert row_has_multi_visual_band(row)
    split = split_row_by_visual_bands(row)
    assert len(split) == 2
    band_texts = [[c.text for c in band if c.text] for band in split]
    assert "wrong limit" in band_texts[0]
    assert "Al compound" in band_texts[1]


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        (r"\cdot/\Delta mg/m^{3}", "mg/m3"),
        ("1mg/m^{3(R)}", "1 mg/m3"),
        ("f/ml", "f/ml"),
        ("rr~mg/m^{3(l)}", "mg/m3"),
    ],
)
def test_latex_limit_normalization_for_search_only(raw: str, expected_fragment: str):
    normalized = normalize_match_text(raw)
    assert expected_fragment in normalized
    assert raw != normalized or expected_fragment in normalized


def test_resolve_bbox_inputs_from_cell_dict():
    cell = {
        "bbox": {"x": 1, "y": 129, "width": 10, "height": 10},
        "bbox_source": "pymupdf_aligned",
        "source": "document_ai",
    }
    da_bbox, provenance = resolve_bbox_inputs_from_cell(cell)
    assert da_bbox is None
    assert provenance is None


def test_trusted_document_ai_passthrough_requires_provenance(tmp_path: Path):
    pdf_path = tmp_path / "passthrough.pdf"
    _write_pdf(pdf_path, ("ignored", 100, 100))
    bbox = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 12.0}
    with GeometryResolver(pdf_path) as resolver:
        trusted = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="ignored", table_id="t1"),
            document_ai_bbox=bbox,
            bbox_provenance=BBOX_SOURCE_DOCUMENT_AI,
        )
        untrusted = resolver.resolve(
            GeometryCellInput(page_number=1, cell_text="ignored", table_id="t1"),
            document_ai_bbox=bbox,
            bbox_provenance=BBOX_PROVENANCE_PYMUPDF_ALIGNED,
        )
    assert trusted.match_method == MATCH_EXACT
    assert trusted.bbox_source == BBOX_SOURCE_DOCUMENT_AI
    assert untrusted.bbox_source == BBOX_SOURCE_PYMUPDF


@pytest.mark.skipif(not OHE6_PDF.exists(), reason="OHE6.pdf not available")
def test_page50_vs_cells_do_not_trust_recovery_bbox_in_validation_helper():
    import json

    vs_path = PROJECT_ROOT / "data/intermediate/validated_structure_46-70.json"
    if not vs_path.exists():
        pytest.skip("validated_structure batch not available")
    data = json.loads(vs_path.read_text(encoding="utf-8"))
    recovery_as_da = 0
    trusted = 0
    for table in data.get("tables", []):
        if int(table["page_number"]) != 50:
            continue
        for row in table.get("rows", []):
            for cell in row:
                bbox = cell.get("bbox")
                bbox_source = cell.get("bbox_source")
                if is_valid_bbox(bbox):
                    da_bbox, _ = resolve_bbox_inputs_from_cell(cell)
                    if da_bbox is not None:
                        trusted += 1
                    elif cell.get("source") == "document_ai":
                        recovery_as_da += 1
    assert recovery_as_da > 0, "fixture expects legacy page-50 recovery bboxes"
    assert trusted == 0, "legacy recovery bboxes must not be trusted as Document AI"
