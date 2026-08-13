"""Unit tests for PyMuPDF page geometry indexing."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from ingestion.pdf_geometry import (
    PageGeometryIndex,
    bbox_y_center,
    extract_cas_numbers,
    is_numeric_only_text,
    is_short_limit_token,
    normalize_match_text,
    rect_to_bbox,
    tokenize_match_text,
    union_word_bbox,
)


def _write_pdf(path: Path, *lines: tuple[str, float, float]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def test_page_geometry_index_extracts_words_and_lines(tmp_path: Path):
    pdf_path = tmp_path / "geometry.pdf"
    _write_pdf(
        pdf_path,
        ("Acetaldehyde [75-07-0]", 100, 120),
        ("TWA", 300, 80),
    )

    with fitz.open(pdf_path) as doc:
        index = PageGeometryIndex(doc[0])

    assert index.page_number == 1
    assert index.page_width > 0
    assert len(index.words) >= 2
    assert len(index.lines) >= 1
    assert any("acetaldehyde" in word.norm for word in index.words)
    assert any("twa" in word.norm for word in index.words)


def test_words_by_line_groups_by_y_coordinate(tmp_path: Path):
    pdf_path = tmp_path / "lines.pdf"
    _write_pdf(
        pdf_path,
        ("استون", 400, 150),
        ("Acetone", 300, 150),
        ("TWA", 100, 80),
    )

    with fitz.open(pdf_path) as doc:
        index = PageGeometryIndex(doc[0])

    grouped = index.words_by_line()
    assert grouped
    assert any("acetone" in word.norm for word in index.words)


def test_union_word_bbox_returns_xywh():
    with fitz.open() as doc:
        page = doc.new_page(width=200, height=200)
        page.insert_text((10, 20), "A", fontsize=11)
        page.insert_text((40, 25), "B", fontsize=11)
        index = PageGeometryIndex(page)

    bbox = union_word_bbox(index.words[:2])
    assert bbox["width"] > 0
    assert bbox["height"] > 0
    assert bbox["x"] <= bbox["x"] + bbox["width"]


def test_rect_to_bbox_converts_fitz_rect():
    rect = fitz.Rect(10, 20, 60, 35)
    bbox = rect_to_bbox(rect)
    assert bbox == {"x": 10.0, "y": 20.0, "width": 50.0, "height": 15.0}


def test_extract_cas_numbers_handles_spaced_brackets():
    assert extract_cas_numbers("Styrene [ 100-42-5 ]") == ["[100-42-5]"]


def test_extract_cas_numbers_repairs_reversed_ocr_brackets():
    assert extract_cas_numbers("لیندان\n9] - 89 - [58 Lindane") == ["[58-89-9]"]


def test_normalize_and_tokenize_persian_digits():
    assert "123" in normalize_match_text("۱۲۳ ppm")
    assert tokenize_match_text("۰/۵ ppm") == ["0", "5", "ppm"]


def test_numeric_and_short_token_helpers():
    assert is_numeric_only_text("44/05")
    assert is_numeric_only_text("۲")
    assert not is_numeric_only_text("10 ppm")
    assert is_short_limit_token("TWA")
    assert is_short_limit_token("ppm")
    assert not is_short_limit_token("10 ppm")


def test_bbox_y_center_helper():
    assert bbox_y_center({"x": 0, "y": 10, "width": 20, "height": 8}) == 14.0
