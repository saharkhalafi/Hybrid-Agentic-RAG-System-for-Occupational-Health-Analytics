"""Tests for structural confidence scoring."""

from document_ai.schema import ExtractionCell, ExtractionRow
from validation.structural_confidence import compute_structural_confidence


def test_structural_confidence_for_consistent_table():
    rows = [
        ExtractionRow(cells=[
            ExtractionCell(text="CAS", bbox=None, confidence=None, row_index=0, column_index=0),
            ExtractionCell(text="TWA", bbox=None, confidence=None, row_index=0, column_index=1),
        ]),
        ExtractionRow(cells=[
            ExtractionCell(text="67-56-1", bbox=None, confidence=None, row_index=1, column_index=0),
            ExtractionCell(text="200", bbox=None, confidence=None, row_index=1, column_index=1),
        ]),
    ]
    score = compute_structural_confidence(rows)
    assert score >= 0.8


def test_structural_confidence_penalizes_empty_cells():
    rows = [
        ExtractionRow(cells=[
            ExtractionCell(text="", bbox=None, confidence=None, row_index=0, column_index=0),
            ExtractionCell(text="", bbox=None, confidence=None, row_index=0, column_index=1),
        ]),
    ]
    score = compute_structural_confidence(rows)
    assert score <= 0.55
