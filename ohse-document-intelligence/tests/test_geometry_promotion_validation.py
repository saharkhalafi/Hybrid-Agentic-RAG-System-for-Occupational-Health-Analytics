"""Tests for geometry promotion validation helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from scripts.run_geometry_promotion_validation import search_hits_span_row_bands


def _write_pdf(path: Path, *lines: tuple[str, float, float]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    doc.save(path)
    doc.close()


def test_search_hits_span_row_bands_true_for_cross_row_duplicates(tmp_path: Path):
    pdf_path = tmp_path / "dup.pdf"
    _write_pdf(pdf_path, ("ppm 1", 100, 120), ("ppm 1", 100, 320))
    with fitz.open(pdf_path) as doc:
        assert search_hits_span_row_bands(doc[0], "ppm 1", selected_y=125.0)


def test_search_hits_span_row_bands_false_for_same_row_cluster(tmp_path: Path):
    pdf_path = tmp_path / "same_row.pdf"
    _write_pdf(pdf_path, ("ppm", 100, 220), ("1", 140, 220))
    with fitz.open(pdf_path) as doc:
        assert not search_hits_span_row_bands(doc[0], "ppm", selected_y=225.0)
