"""Persist evidence pipeline output to PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from config.logging import get_logger
from database.models import (
    ChemicalRegistry,
    Document,
    DocumentEvidenceSnapshot,
    DocumentPage,
    ExtractedTable,
    OELChemicalLimit,
    TableCell,
    TableType,
    ValidatedTableRow,
)
from goldset_generator.fact_resolver import resolve_field_display
from goldset_generator.structural_resolver import StructuralResolverResult
from goldset_generator.table_gold_generator import TableGoldGenerator
from schema_registry.registry import get_schema_registry
from validation_engine.engine import ValidationEngine

logger = get_logger(__name__)

PERSIAN_DIGIT = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = str(value).translate(PERSIAN_DIGIT)
    match = re.search(r"[\d]+(?:[./][\d]+)?", cleaned)
    if not match:
        return None
    token = match.group().replace("/", ".")
    try:
        return float(token)
    except ValueError:
        return None


def _content_hash(filename: str, start: int, end: int, base_hash: str) -> str:
    key = f"{filename}:{base_hash}:{start}-{end}"
    return hashlib.sha256(key.encode()).hexdigest()


def _is_merged_cell(text: str | None, ref: dict[str, Any]) -> bool:
    """Return True when a cell spans merged OCR content."""
    if ref.get("value_status") == "merged_cell":
        return True
    if not text:
        return False
    return len(re.findall(r"\[\d{2,7}-\d{2}-\d\]", text)) > 1


def upsert_document(
    session: Session,
    *,
    filename: str,
    content_hash: str,
    page_count: int,
    processing_version: str,
) -> Document:
    existing = session.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        session.execute(delete(Document).where(Document.id == existing.id))
        session.flush()
    document = Document(
        filename=filename,
        content_hash=content_hash,
        language="fa,en",
        processing_version=processing_version,
        page_count=page_count,
        metadata_={"pipeline": "evidence_v2"},
    )
    session.add(document)
    session.flush()
    return document


def persist_evidence_snapshot(
    session: Session,
    document: Document,
    *,
    page_start: int,
    page_end: int,
    raw_json_path: Path,
    processor_format: str,
) -> DocumentEvidenceSnapshot | None:
    if not raw_json_path.exists():
        return None
    content = raw_json_path.read_bytes()
    snapshot_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(DocumentEvidenceSnapshot).where(
            DocumentEvidenceSnapshot.document_id == document.id,
            DocumentEvidenceSnapshot.page_start == page_start,
            DocumentEvidenceSnapshot.page_end == page_end,
            DocumentEvidenceSnapshot.snapshot_hash == snapshot_hash,
        )
    )
    if existing:
        logger.info("evidence_snapshot_unchanged", snapshot_hash=snapshot_hash[:12])
        return existing

    snapshot = DocumentEvidenceSnapshot(
        document_id=document.id,
        page_start=page_start,
        page_end=page_end,
        snapshot_hash=snapshot_hash,
        storage_path=str(raw_json_path),
        processor_format=processor_format,
        immutable=True,
    )
    session.add(snapshot)
    return snapshot


def persist_universal_tables(
    session: Session,
    document: Document,
    structural: StructuralResolverResult,
    page_detection: dict[int, dict[str, Any]],
) -> dict[str, uuid.UUID]:
    """Persist Layer 1+2 tables/cells with stable evidence_cell_id."""
    stable_to_uuid: dict[str, uuid.UUID] = {}

    for table in structural.tables:
        detection = page_detection.get(table.page_number, {})
        try:
            table_type = TableType(table.table_type)
        except ValueError:
            table_type = TableType.UNKNOWN

        schema = get_schema_registry().for_table_type(table.table_type)
        schema_id = schema["schema_id"] if schema else None

        db_table = ExtractedTable(
            document_id=document.id,
            page_number=table.page_number,
            table_type=table_type,
            stable_table_id=table.table_id,
            schema_id=schema_id,
            recovery_method=detection.get("recovery_method"),
            raw_json=table.to_dict(),
            raw_markdown=table.raw_markdown,
            confidence=table.structural_confidence,
            source_processor=detection.get("table_detection_status", "document_ai"),
            bbox=table.bbox,
        )
        session.add(db_table)
        session.flush()
        stable_to_uuid[table.table_id] = db_table.id

        for cell in table.flat_cells():
            ref = cell.source_reference or {}
            session.add(
                TableCell(
                    table_id=db_table.id,
                    page_number=cell.page_number,
                    row_index=cell.row,
                    column_index=cell.column,
                    raw_text=cell.text,
                    original_value=cell.text,
                    normalized_value=cell.normalized_value,
                    bbox=cell.bbox,
                    confidence=cell.confidence,
                    bbox_source=cell.bbox_source,
                    bbox_confidence=cell.bbox_confidence,
                    source_reference={
                        **ref,
                        "evidence_cell_id": cell.cell_id,
                    },
                    evidence_cell_id=cell.cell_id,
                    merged_cell=_is_merged_cell(cell.text, ref),
                    source=cell.source,
                )
            )
    return stable_to_uuid


def persist_validated_rows_and_domain(
    session: Session,
    document: Document,
    table_golds: list[dict[str, Any]],
    stable_to_uuid: dict[str, uuid.UUID],
) -> dict[str, int]:
    """Persist validated semantic rows and OEL domain records."""
    validator = ValidationEngine()
    stats = {"validated_rows": 0, "oel_limits": 0, "review_rows": 0}

    for table_gold in table_golds:
        stable_id = table_gold["table_id"]
        db_table_id = stable_to_uuid.get(stable_id)
        if not db_table_id:
            continue

        schema = get_schema_registry().for_table_type(table_gold.get("table_type", "chemical_oel"))
        schema_id = schema["schema_id"] if schema else "unknown"

        for row_index, row in enumerate(table_gold.get("rows") or [], start=1):
            validation = validator.validate_table_row(
                table_gold.get("table_type", "chemical_oel"),
                row,
                schema_id=schema_id,
            )

            field_provenance = {}
            row_data = {}
            for field_name, field_data in row.items():
                if not isinstance(field_data, dict):
                    continue
                display = resolve_field_display(field_name, field_data)
                row_data[field_name] = display
                field_provenance[field_name] = {
                    "cell_id": field_data.get("cell_id"),
                    "bbox": field_data.get("bbox"),
                    "value_status": field_data.get("value_status"),
                    "original_value": field_data.get("original_value"),
                }

            subject = (
                row_data.get("persian_chemical_name")
                or row_data.get("chemical_name")
                or f"row_{row_index}"
            )

            session.add(
                ValidatedTableRow(
                    table_id=db_table_id,
                    document_id=document.id,
                    page_number=table_gold["page_number"],
                    row_index=row_index,
                    schema_id=schema_id,
                    subject=subject,
                    row_data=row_data,
                    field_provenance=field_provenance,
                    confidence=validation.confidence.overall,
                    requires_review=validation.requires_review,
                    validation_issues=[issue.to_dict() for issue in validation.issues] or None,
                )
            )
            stats["validated_rows"] += 1
            if validation.requires_review:
                stats["review_rows"] += 1

            if table_gold.get("table_type") != "chemical_oel":
                continue

            cas_value = resolve_field_display("CAS", row.get("CAS") or row.get("chemical_name") or {})
            if not cas_value:
                continue

            chemical = session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas_value))
            if not chemical:
                chemical = ChemicalRegistry(
                    cas=cas_value,
                    english_name=row_data.get("chemical_name"),
                    persian_name=row_data.get("persian_chemical_name"),
                    molecular_weight=_parse_float(row_data.get("molecular_weight")),
                )
                session.add(chemical)
                session.flush()

            twa_field = row.get("TWA") or {}
            stel_field = row.get("STEL") or {}
            ceiling_field = row.get("ceiling") or {}

            twa_val = _parse_float(resolve_field_display("TWA", twa_field) or twa_field.get("value"))
            stel_val = _parse_float(resolve_field_display("STEL", stel_field) or stel_field.get("value"))
            ceiling_val = _parse_float(
                resolve_field_display("ceiling", ceiling_field) or ceiling_field.get("value")
            )
            unit = twa_field.get("unit") or stel_field.get("unit") or ceiling_field.get("unit")

            session.add(
                OELChemicalLimit(
                    chemical_id=chemical.id,
                    twa=twa_val,
                    stel=stel_val,
                    ceiling=ceiling_val,
                    unit=unit,
                    page_number=table_gold["page_number"],
                    persian_name=row_data.get("persian_chemical_name"),
                    english_name=row_data.get("chemical_name"),
                    standard_reference="OHE6",
                    source_table_id=db_table_id,
                    source_cell_provenance=field_provenance,
                    original_values=row_data,
                    confidence=validation.confidence.overall,
                )
            )
            stats["oel_limits"] += 1

    return stats
