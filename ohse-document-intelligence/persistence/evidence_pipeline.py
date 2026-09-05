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

from agents.structured.store import CANONICAL_GOLD_ARTIFACT_PATH
from config.logging import get_logger
from config.settings import get_settings
from database.models import (
    ChemicalRegistry,
    ChunkType,
    Document,
    DocumentChunk,
    DocumentEvidenceSnapshot,
    DocumentPage,
    ExtractedTable,
    Formula,
    OELChemicalLimit,
    PageType,
    ReviewIssueType,
    ReviewQueueItem,
    TableCell,
    TableType,
    ValidatedTableRow,
)
from document_ai.geometry_gating import CellGeometryGate, evaluate_cell_geometry_gate
from goldset_generator.fact_resolver import resolve_field_display
from persistence.row_knowledge_text import gold_row_knowledge_content
from goldset_generator.structural_resolver import StructuralResolverResult
from goldset_generator.table_gold_generator import TableGoldGenerator
from schema_registry.registry import get_schema_registry
from validation_engine.engine import ValidationEngine
from review.review_queue import enqueue_review

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


def upsert_production_document(
    session: Session,
    *,
    filename: str,
    content_hash: str,
    page_count: int,
    processing_version: str,
) -> Document:
    """Find or create the production document identity. Does not delete existing rows."""
    existing = session.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        existing.filename = filename
        existing.page_count = page_count
        existing.processing_version = processing_version
        metadata = dict(existing.metadata_ or {})
        metadata["pipeline"] = "production_ingestion_v1"
        existing.metadata_ = metadata
        session.flush()
        return existing
    document = Document(
        filename=filename,
        content_hash=content_hash,
        language="fa,en",
        processing_version=processing_version,
        page_count=page_count,
        metadata_={"pipeline": "production_ingestion_v1"},
    )
    session.add(document)
    session.flush()
    return document


def replace_document_extraction(
    session: Session,
    document: Document,
    *,
    page_start: int,
    page_end: int,
) -> None:
    """Remove this document's extracted artifacts in the ingest range before a clean persist."""
    table_ids = list(
        session.scalars(
            select(ExtractedTable.id).where(
                ExtractedTable.document_id == document.id,
                ExtractedTable.page_number.between(page_start, page_end),
            )
        ).all()
    )
    if table_ids:
        session.execute(delete(OELChemicalLimit).where(OELChemicalLimit.source_table_id.in_(table_ids)))
        session.execute(delete(ValidatedTableRow).where(ValidatedTableRow.table_id.in_(table_ids)))
        session.execute(delete(Formula).where(Formula.table_id.in_(table_ids)))
        session.execute(delete(ExtractedTable).where(ExtractedTable.id.in_(table_ids)))
    session.execute(
        delete(OELChemicalLimit).where(
            OELChemicalLimit.page_number.between(page_start, page_end),
            OELChemicalLimit.source_table_id.is_(None),
        )
    )
    session.execute(
        delete(Formula).where(
            Formula.document_id == document.id,
            Formula.page_number.between(page_start, page_end),
        )
    )
    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.page_number.between(page_start, page_end),
        )
    )
    session.execute(
        delete(DocumentPage).where(
            DocumentPage.document_id == document.id,
            DocumentPage.page_number.between(page_start, page_end),
        )
    )
    session.execute(
        delete(ReviewQueueItem).where(
            ReviewQueueItem.document_id == document.id,
            ReviewQueueItem.page_number.between(page_start, page_end),
        )
    )
    session.execute(
        delete(DocumentEvidenceSnapshot).where(
            DocumentEvidenceSnapshot.document_id == document.id,
            DocumentEvidenceSnapshot.page_start == page_start,
            DocumentEvidenceSnapshot.page_end == page_end,
        )
    )
    session.flush()


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
    *,
    bbox_confidence_threshold: float | None = None,
) -> dict[str, uuid.UUID]:
    """Persist Layer 1+2 tables/cells with stable evidence_cell_id."""
    stable_to_uuid: dict[str, uuid.UUID] = {}
    threshold = (
        bbox_confidence_threshold
        if bbox_confidence_threshold is not None
        else get_settings().bbox_confidence_threshold
    )

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
        pending_reviews: list[tuple[Any, Any, Any, dict[str, Any], str]] = []

        for cell in table.flat_cells():
            ref = cell.source_reference or {}
            gate = evaluate_cell_geometry_gate(
                resolved_bbox=cell.bbox,
                bbox_confidence=cell.bbox_confidence,
                threshold=threshold,
            )

            db_cell = TableCell(
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
                        "geometry_gate": gate.gate.value,
                    },
                    evidence_cell_id=cell.cell_id,
                    merged_cell=_is_merged_cell(cell.text, ref),
                    source=cell.source,
                )
            session.add(db_cell)
            pending_reviews.append((db_cell, gate, cell, ref, table.table_id))

        session.flush()
        for db_cell, gate, cell, ref, stable_table_id in pending_reviews:
            if gate.gate is CellGeometryGate.MISSING_BBOX:
                enqueue_review(
                    session,
                    object_type="table_cell",
                    object_id=db_cell.id,
                    document_id=document.id,
                    issue_type=ReviewIssueType.MISSING_BBOX,
                    page_number=cell.page_number,
                    bbox=None,
                    confidence=cell.confidence,
                    review_result={
                        "table_id": str(db_table.id),
                        "stable_table_id": stable_table_id,
                        "row_index": cell.row,
                        "column_index": cell.column,
                        "text": cell.text,
                        "evidence_cell_id": cell.cell_id,
                        "match_method": ref.get("match_method"),
                        "geometry_gate": gate.gate.value,
                    },
                )
            elif gate.gate is CellGeometryGate.LOW_CONFIDENCE:
                enqueue_review(
                    session,
                    object_type="table_cell",
                    object_id=db_cell.id,
                    document_id=document.id,
                    issue_type=ReviewIssueType.LOW_BBOX_CONFIDENCE,
                    page_number=cell.page_number,
                    bbox=cell.bbox,
                    confidence=cell.bbox_confidence,
                    review_result={
                        "table_id": str(db_table.id),
                        "stable_table_id": stable_table_id,
                        "row_index": cell.row,
                        "column_index": cell.column,
                        "text": cell.text,
                        "evidence_cell_id": cell.cell_id,
                        "bbox_source": cell.bbox_source,
                        "bbox_confidence": cell.bbox_confidence,
                        "source_reference": ref,
                        "geometry_gate": gate.gate.value,
                    },
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

            twa_val, twa_unit = _limit_from_field(twa_field, "TWA")
            stel_val, stel_unit = _limit_from_field(stel_field, "STEL")
            ceiling_val, ceiling_unit = _limit_from_field(ceiling_field, "ceiling")
            unit = twa_unit or stel_unit or ceiling_unit
            source_row_key = f"{stable_id}:row_{row_index}"
            accepted = not validation.requires_review

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
                    source_table_id_str=stable_id,
                    source_row_key=source_row_key,
                    source_cell_provenance=field_provenance,
                    original_values=row_data,
                    accepted_values=(
                        {"twa": twa_val, "stel": stel_val, "ceiling": ceiling_val, "unit": unit}
                        if accepted
                        else None
                    ),
                    validation_status="accepted" if accepted else "pending_review",
                    gold_artifact_path=CANONICAL_GOLD_ARTIFACT_PATH if accepted else None,
                    confidence=validation.confidence.overall,
                )
            )
            stats["oel_limits"] += 1

    return stats


def _limit_from_field(field: dict[str, Any], field_name: str) -> tuple[float | None, str | None]:
    if not isinstance(field, dict):
        return None, None
    status = field.get("value_status")
    unit = field.get("unit")
    if status in {"absent", "merged_cell", "extraction_uncertain"}:
        return None, unit
    display = resolve_field_display(field_name, field) or field.get("value")
    return _parse_float(display), unit


def persist_document_pages(
    session: Session,
    document: Document,
    pages: list[Any],
    page_detection: dict[int, dict[str, Any]],
) -> int:
    from ingestion.page_classifier import classify_page
    from ingestion.pdf_loader import PageContent

    count = 0
    for page in pages:
        page_number = page.page_number
        detection = page_detection.get(page_number) or {}
        content = PageContent(
            page_number=page_number,
            text=page.text or "",
            has_digital_text=bool(page.has_digital_text),
            width=0.0,
            height=0.0,
        )
        triage = classify_page(content)
        try:
            page_type = PageType(triage.page_type.value)
        except ValueError:
            page_type = PageType.UNKNOWN
        session.add(
            DocumentPage(
                document_id=document.id,
                page_number=page_number,
                raw_text=page.text,
                layout_json={"paragraphs": page.paragraphs},
                page_type=page_type,
                confidence=triage.confidence,
                has_digital_text=bool(page.has_digital_text),
                triage_metadata={
                    **triage.metadata,
                    "page_detection": detection,
                    "printed_page_number": page.printed_page_number,
                    "table_score": triage.table_score,
                    "formula_score": triage.formula_score,
                },
            )
        )
        count += 1
    session.flush()
    return count


def persist_extracted_formulas(
    session: Session,
    document: Document,
    formula_records: list[dict[str, Any]],
    *,
    stable_to_uuid: dict[str, uuid.UUID] | None = None,
) -> dict[str, int]:
    stats = {"formulas_persisted": 0, "formulas_review": 0, "formulas_skipped_empty": 0}
    stable_to_uuid = stable_to_uuid or {}
    for payload in formula_records:
        evidence = payload.get("evidence") or {}
        reconstruction = payload.get("reconstruction") or {}
        original = payload.get("raw_expression") or reconstruction.get("raw_expression") or ""
        normalized = payload.get("normalized_expression") or reconstruction.get("expression") or ""
        if not str(original).strip() and not str(normalized).strip():
            stats["formulas_skipped_empty"] += 1
            continue
        page = evidence.get("page") or (payload.get("source_reference") or {}).get("page") or 0
        stable_id = payload.get("formula_id")
        status = payload.get("status")
        validation_status = "accepted" if status == "approved" else "extraction_uncertain"
        if validation_status != "accepted":
            stats["formulas_review"] += 1
        existing = session.scalar(select(Formula).where(Formula.stable_formula_id == stable_id))
        record = existing or Formula(
            id=uuid.uuid4(),
            stable_formula_id=stable_id,
            document_id=document.id,
        )
        record.document_id = document.id
        record.formula_name = stable_id
        record.original_expression = original
        record.normalized_expression = normalized or original
        record.variables = (payload.get("semantics") or {}).get("variables")
        record.page_number = int(page or 0)
        record.bbox = evidence.get("bbox")
        record.confidence = (payload.get("confidence") or {}).get("overall")
        record.validation_status = validation_status
        record.source_reference = payload.get("source_reference") or evidence
        record.semantics = payload.get("semantics")
        record.knowledge_metadata = {
            "pipeline": "production_ingestion_v1",
            "status": status,
            "validation": payload.get("validation"),
        }
        if not existing:
            session.add(record)
        stats["formulas_persisted"] += 1
    session.flush()
    return stats


def persist_semantic_and_row_chunks(
    session: Session,
    document: Document,
    *,
    semantic_records: list[dict[str, Any]],
    table_golds: list[dict[str, Any]],
) -> dict[str, int]:
    from retrieval.semantic_retrieval import SEMANTIC_SOURCE_TYPE

    stats = {"semantic_chunks": 0, "semantic_review": 0, "row_knowledge_chunks": 0}
    for record in semantic_records:
        content = (
            record.get("text")
            or record.get("normalized_text")
            or record.get("raw_text")
            or ""
        ).strip()
        if not content:
            continue
        review_status = record.get("review_status") or "pending"
        accepted = review_status != "review_required"
        if not accepted:
            stats["semantic_review"] += 1
        page_number = record.get("pdf_page_number") or record.get("page_number")
        bbox = record.get("bbox")
        session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_id=record.get("chunk_id"),
                section=(record.get("section") or {}).get("title")
                if isinstance(record.get("section"), dict)
                else record.get("section"),
                section_title=(record.get("section") or {}).get("title")
                if isinstance(record.get("section"), dict)
                else None,
                content=content,
                chunk_type=ChunkType.PARAGRAPH,
                page_number=page_number,
                printed_page_number=record.get("printed_page_number"),
                language="fa",
                source_type=SEMANTIC_SOURCE_TYPE,
                confidence=0.9 if accepted else 0.5,
                validation_status="accepted" if accepted else "review_required",
                provenance={
                    "page_number": page_number,
                    "bbox": bbox,
                    "text_evidence_ids": (record.get("source_references") or {}).get("text_evidence_ids"),
                    "table_ids": (record.get("source_references") or {}).get("table_ids"),
                    "formula_ids": (record.get("source_references") or {}).get("formula_ids"),
                },
                metadata_=record,
            )
        )
        stats["semantic_chunks"] += 1

    for table_gold in table_golds:
        page_number = table_gold.get("page_number")
        table_id = table_gold.get("table_id")
        for row_index, row in enumerate(table_gold.get("rows") or [], start=1):
            provenance: dict[str, Any] = {}
            gold_row = row if isinstance(row, dict) else {}
            for field_name, field_data in gold_row.items():
                if not isinstance(field_data, dict):
                    continue
                provenance[field_name] = {
                    "cell_id": field_data.get("cell_id"),
                    "bbox": field_data.get("bbox"),
                    "value_status": field_data.get("value_status"),
                    "page_number": page_number,
                }
            content = gold_row_knowledge_content(gold_row)
            if not content.strip():
                continue
            chunk_id = f"row_{table_id}_{row_index:03d}"
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_id=chunk_id,
                    section=table_id,
                    topic="row_knowledge",
                    content=content,
                    chunk_type=ChunkType.TABLE_CONTEXT,
                    page_number=page_number,
                    language="fa,en",
                    source_type="row_knowledge",
                    confidence=0.9,
                    validation_status="accepted",
                    gold_artifact_path=CANONICAL_GOLD_ARTIFACT_PATH,
                    provenance={"table_id": table_id, "row_index": row_index, "fields": provenance},
                    metadata_={"table_id": table_id, "row_index": row_index, "page_number": page_number},
                )
            )
            stats["row_knowledge_chunks"] += 1
    session.flush()
    return stats
