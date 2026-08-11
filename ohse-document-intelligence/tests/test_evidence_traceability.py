"""Verify evidence traceability for extracted cells."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from database.models import Document, ExtractedTable, TableCell
from database.session import session_scope
from normalization.persian_normalizer import normalize_persian_text


def get_cell_evidence(session, cell_id: uuid.UUID) -> dict | None:
    """Return full evidence chain for a table cell."""
    cell = session.get(TableCell, cell_id)
    if not cell:
        return None

    table = session.get(ExtractedTable, cell.table_id)
    if not table:
        return None

    document = session.get(Document, table.document_id)
    if not document:
        return None

    return {
        "document": document.filename,
        "document_id": str(document.id),
        "page_number": cell.page_number,
        "table_id": str(cell.table_id),
        "cell_id": str(cell.id),
        "bbox": cell.bbox,
        "original_ocr_value": cell.original_value,
        "normalized_value": cell.normalized_value,
        "confidence": cell.confidence,
    }


def test_cell_evidence_traceability_roundtrip():
    """Insert a representative chemical concentration cell and verify traceability."""
    with session_scope() as session:
        document = Document(
            filename="traceability-fixture.pdf",
            content_hash=f"traceability-test-{uuid.uuid4().hex}",
            language="fa,en",
            processing_version="test",
            page_count=1,
        )
        session.add(document)
        session.flush()

        table = ExtractedTable(
            document_id=document.id,
            page_number=47,
            table_type="chemical_oel",
            raw_json={"source": "test"},
            raw_markdown="| Acrolein | 0.5 | ppm |",
            confidence=0.92,
            bbox={"x": 100, "y": 200, "width": 50, "height": 12},
        )
        session.add(table)
        session.flush()

        normalized = normalize_persian_text("۰/۵ ppm")
        cell = TableCell(
            table_id=table.id,
            page_number=47,
            row_index=1,
            column_index=2,
            raw_text="۰/۵ ppm",
            original_value=normalized.original,
            normalized_value=normalized.normalized,
            unit="ppm",
            bbox={"x": 100, "y": 200, "width": 50, "height": 12},
            confidence=0.91,
            bbox_source="pymupdf",
            bbox_confidence=0.98,
            source_reference={
                "document_path": "traceability-fixture.pdf",
                "page_number": 47,
                "match_method": "exact",
            },
        )
        session.add(cell)
        session.flush()

        cell_id = cell.id

    with session_scope() as session:
        evidence = get_cell_evidence(session, cell_id)

    assert evidence is not None
    assert evidence["document"] == "traceability-fixture.pdf"
    assert evidence["page_number"] == 47
    assert evidence["table_id"] is not None
    assert evidence["cell_id"] == str(cell_id)
    assert evidence["bbox"] is not None
    assert evidence["original_ocr_value"] == "۰/۵ ppm"
    assert "0.5" in evidence["normalized_value"] or "0" in evidence["normalized_value"]
    assert evidence["confidence"] == 0.91


def test_real_extraction_cells_populated_for_page_range_runs():
    """Chemical section page-range runs should populate table_cells via geometry resolver."""
    import hashlib

    base_hash = "8b9e2240e437037dd0d4d02e3f5642ad0f723c41e489364853b09b58252bd2a2"
    content_hash = hashlib.sha256(f"{base_hash}:46-55".encode()).hexdigest()

    with session_scope() as session:
        doc = session.scalar(select(Document).where(Document.content_hash == content_hash))
        if not doc:
            pytest.skip("No OHE6 pages 46-55 document in database")

        from sqlalchemy import func

        cell_count = session.scalar(
            select(func.count())
            .select_from(TableCell)
            .join(ExtractedTable)
            .where(ExtractedTable.document_id == doc.id)
        )
        assert cell_count > 0, "Expected geometry-resolved cells for pages 46-55"
