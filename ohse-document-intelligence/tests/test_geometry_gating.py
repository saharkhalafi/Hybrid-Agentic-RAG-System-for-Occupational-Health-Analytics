"""Tests for shared table-cell geometry persistence gating."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from document_ai.geometry_gating import CellGeometryGate, evaluate_cell_geometry_gate
from persistence.evidence_pipeline import persist_universal_tables
from goldset_generator.document_processor import ExtractedCellRecord, ExtractedTableRecord
from goldset_generator.structural_resolver import StructuralResolverResult


def test_evaluate_cell_geometry_gate_accept():
    result = evaluate_cell_geometry_gate(
        resolved_bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        bbox_confidence=0.9,
        threshold=0.85,
    )
    assert result.gate is CellGeometryGate.ACCEPT
    assert result.persist is True


def test_evaluate_cell_geometry_gate_missing_bbox():
    result = evaluate_cell_geometry_gate(
        resolved_bbox=None,
        bbox_confidence=0.95,
        threshold=0.85,
    )
    assert result.gate is CellGeometryGate.MISSING_BBOX
    assert result.persist is False


def test_evaluate_cell_geometry_gate_low_confidence():
    result = evaluate_cell_geometry_gate(
        resolved_bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        bbox_confidence=0.5,
        threshold=0.85,
    )
    assert result.gate is CellGeometryGate.LOW_CONFIDENCE
    assert result.persist is False


def test_persist_universal_tables_persists_low_confidence_cells():
    session = MagicMock()
    document = MagicMock()
    document.id = uuid.uuid4()

    cell = ExtractedCellRecord(
        cell_id="cell_1_1_2",
        table_id="table_46_01",
        page_number=46,
        row=1,
        column=2,
        text="25 ppm",
        bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        confidence=0.9,
        bbox_confidence=0.5,
        bbox_source="pymupdf",
        source="document_ai",
        normalized_value="25 ppm",
        source_reference={"match_method": "token"},
    )
    table = ExtractedTableRecord(
        table_id="table_46_01",
        page_number=46,
        table_type="chemical_oel",
        rows=[[cell]],
        structural_confidence=0.9,
        raw_markdown="| 25 ppm |",
        bbox=None,
    )
    structural = StructuralResolverResult(tables=[table])

    persist_universal_tables(session, document, structural, page_detection={})

    added_cells = [call.args[0] for call in session.add.call_args_list if hasattr(call.args[0], "raw_text")]
    assert len(added_cells) == 1
    assert added_cells[0].bbox_confidence == 0.5
    assert added_cells[0].source_reference["geometry_gate"] == "low_confidence"


def test_persist_universal_tables_accepts_high_confidence_cells():
    session = MagicMock()
    document = MagicMock()
    document.id = uuid.uuid4()

    cell = ExtractedCellRecord(
        cell_id="cell_1_1_2",
        table_id="table_46_01",
        page_number=46,
        row=1,
        column=2,
        text="25 ppm",
        bbox={"x": 1, "y": 2, "width": 3, "height": 4},
        confidence=0.9,
        bbox_confidence=0.95,
        bbox_source="pymupdf",
        source="document_ai",
        normalized_value="25 ppm",
        source_reference={"match_method": "exact"},
    )
    table = ExtractedTableRecord(
        table_id="table_46_01",
        page_number=46,
        table_type="chemical_oel",
        rows=[[cell]],
        structural_confidence=0.9,
        raw_markdown="| 25 ppm |",
        bbox=None,
    )
    structural = StructuralResolverResult(tables=[table])

    persist_universal_tables(session, document, structural, page_detection={})

    added_cells = [call.args[0] for call in session.add.call_args_list if hasattr(call.args[0], "raw_text")]
    assert len(added_cells) == 1
    assert added_cells[0].bbox_confidence == 0.95
