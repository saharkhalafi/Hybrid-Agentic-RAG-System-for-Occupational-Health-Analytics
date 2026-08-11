"""Tests for PDF geometry matching."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from document_ai.geometry_resolver import (
    BBOX_SOURCE_PYMUPDF,
    GeometryCellInput,
    GeometryResolver,
    MATCH_CAS,
    MATCH_EXACT,
    MATCH_TOKENS,
)
from ingestion.pdf_geometry import normalize_match_text, tokenize_match_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OHE6_PDF = PROJECT_ROOT.parent / "OHE6.pdf"


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
    assert result.match_method in {MATCH_EXACT, MATCH_CAS, MATCH_TOKENS}


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
