"""End-to-end document processing pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

import fitz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from database.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    ExtractedTable,
    ExtractionValidationReport,
    Formula,
    PageType,
    ReviewIssueType,
    TableCell,
    TableType,
)
from database.session import session_scope
from document_ai.geometry_resolver import (
    GeometryCellInput,
    GeometryResolver,
    is_valid_bbox,
)
from document_ai.processor import DocumentAIProcessor
from document_ai.schema import DocumentAIExtractionResult, ExtractionTable
from extraction.formula_extractor import extract_formulas
from ingestion.page_classifier import classify_page
from ingestion.pdf_loader import load_pdf
from ingestion.preprocessing import preprocess_page_image
from knowledge.table_classifier import classify_table_text
from normalization.persian_normalizer import normalize_persian_text
from review.review_queue import enqueue_review
from validation.confidence import compute_composite_confidence

logger = get_logger(__name__)
CONFIDENCE_THRESHOLD = 0.65


def _parse_pages_argument(pages: str | int | None) -> tuple[int | None, int | None, int | None]:
    if pages is None:
        return None, None, None
    if isinstance(pages, int):
        return 1, None, pages
    value = str(pages).strip()
    if re.fullmatch(r"\d+\-\d+", value):
        start, end = value.split("-", 1)
        return int(start), int(end), None
    if value.isdigit():
        return 1, None, int(value)
    raise ValueError(f"Invalid --pages value: {pages}")


def _processing_content_hash(base_hash: str, page_start: int, page_end: int | None) -> str:
    end = page_end if page_end is not None else page_start
    key = f"{base_hash}:{page_start}-{end}"
    return hashlib.sha256(key.encode()).hexdigest()


def _write_page_subset(
    source: Path,
    destination: Path,
    page_start: int = 1,
    page_end: int | None = None,
    page_limit: int | None = None,
) -> None:
    with fitz.open(source) as src, fitz.open() as dst:
        start_index = max(page_start, 1) - 1
        if page_end is not None:
            end_index = min(page_end, src.page_count) - 1
        elif page_limit is not None:
            end_index = min(start_index + page_limit - 1, src.page_count - 1)
        else:
            end_index = src.page_count - 1
        dst.insert_pdf(src, from_page=start_index, to_page=end_index)
        dst.save(destination)


def _offset_extraction_pages(parsed: DocumentAIExtractionResult, page_start: int) -> None:
    """Map subset-PDF page numbers back to original document page numbers."""
    if page_start <= 1:
        return
    offset = page_start - 1
    for page in parsed.pages:
        page.page_number += offset
        for table in page.tables:
            table.page_number += offset
            for cell in table.flat_cells():
                if cell.page_number is not None:
                    cell.page_number += offset



def _persist_document(
    session: Session,
    filename: str,
    content_hash: str,
    page_count: int,
) -> Document:
    settings = get_settings()
    existing = session.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        session.execute(delete(Document).where(Document.id == existing.id))
        session.flush()
        logger.info("replaced_existing_document", content_hash=content_hash, previous_id=str(existing.id))

    document = Document(
        filename=filename,
        content_hash=content_hash,
        language=",".join(settings.language_codes),
        processing_version=settings.processing_version,
        page_count=page_count,
    )
    session.add(document)
    session.flush()
    return document


def _persist_page(
    session: Session,
    document: Document,
    page_number: int,
    raw_text: str | None,
    page_type: PageType,
    confidence: float | None,
    triage_metadata: dict,
    image_path: str | None,
    has_digital_text: bool,
    ocr_json: dict | None = None,
    layout_json: dict | None = None,
) -> DocumentPage:
    page = DocumentPage(
        document_id=document.id,
        page_number=page_number,
        raw_text=raw_text,
        ocr_json=ocr_json,
        layout_json=layout_json,
        page_type=page_type,
        confidence=confidence,
        image_path=image_path,
        has_digital_text=has_digital_text,
        triage_metadata=triage_metadata,
    )
    session.add(page)
    return page


def _persist_extracted_table(
    session: Session,
    document: Document,
    table: ExtractionTable,
    geometry_resolver: GeometryResolver | None = None,
    bbox_confidence_threshold: float = 0.85,
) -> tuple[ExtractedTable, int, int, int]:
    table_type = classify_table_text(table.raw_markdown)
    raw_json = {**table.raw_json, "_structural_confidence": table.structural_confidence}
    extracted_table = ExtractedTable(
        document_id=document.id,
        page_number=table.page_number,
        table_type=table_type,
        title=table.title,
        caption=table.caption,
        raw_json=raw_json,
        raw_markdown=table.raw_markdown,
        confidence=table.ocr_confidence,
        bbox=table.bbox,
    )
    session.add(extracted_table)
    session.flush()

    if table_type == TableType.UNKNOWN:
        enqueue_review(
            session,
            object_type="extracted_table",
            object_id=extracted_table.id,
            document_id=document.id,
            issue_type=ReviewIssueType.UNKNOWN_TABLE_TYPE,
            page_number=table.page_number,
            confidence=table.structural_confidence,
        )

    inserted_cells = 0
    missing_bbox_cells = 0
    low_bbox_confidence_cells = 0

    for cell in table.flat_cells():
        geometry_match = None
        if geometry_resolver is not None:
            geometry_match = geometry_resolver.resolve(
                GeometryCellInput(
                    page_number=cell.page_number or table.page_number,
                    cell_text=cell.text,
                    table_id=table.table_id,
                    row_index=cell.row_index,
                    column_index=cell.column_index,
                ),
                document_ai_bbox=cell.bbox,
            )

        resolved_bbox = geometry_match.bbox if geometry_match else cell.bbox
        bbox_confidence = geometry_match.match_confidence if geometry_match else 0.0
        bbox_source = geometry_match.bbox_source if geometry_match else None
        source_reference = geometry_match.source_reference if geometry_match else None

        if not is_valid_bbox(resolved_bbox):
            missing_bbox_cells += 1
            enqueue_review(
                session,
                object_type="table_cell_candidate",
                object_id=uuid.uuid4(),
                document_id=document.id,
                issue_type=ReviewIssueType.MISSING_BBOX,
                page_number=cell.page_number or table.page_number,
                bbox=None,
                confidence=cell.confidence,
                review_result={
                    "table_id": str(extracted_table.id),
                    "row_index": cell.row_index,
                    "column_index": cell.column_index,
                    "text": cell.text,
                    "block_ids": cell.block_ids,
                    "match_method": geometry_match.match_method if geometry_match else None,
                },
            )
            continue

        if bbox_confidence < bbox_confidence_threshold:
            low_bbox_confidence_cells += 1
            enqueue_review(
                session,
                object_type="table_cell_candidate",
                object_id=uuid.uuid4(),
                document_id=document.id,
                issue_type=ReviewIssueType.LOW_BBOX_CONFIDENCE,
                page_number=cell.page_number or table.page_number,
                bbox=resolved_bbox,
                confidence=bbox_confidence,
                review_result={
                    "table_id": str(extracted_table.id),
                    "row_index": cell.row_index,
                    "column_index": cell.column_index,
                    "text": cell.text,
                    "block_ids": cell.block_ids,
                    "bbox_source": bbox_source,
                    "bbox_confidence": bbox_confidence,
                    "source_reference": source_reference,
                },
            )
            continue

        normalized = normalize_persian_text(cell.text)
        session.add(
            TableCell(
                table_id=extracted_table.id,
                page_number=cell.page_number or table.page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                raw_text=cell.text,
                original_value=normalized.original,
                normalized_value=normalized.normalized,
                row_span=cell.row_span,
                column_span=cell.column_span,
                bbox=resolved_bbox,
                confidence=cell.confidence,
                bbox_source=bbox_source,
                bbox_confidence=bbox_confidence,
                source_reference=source_reference,
            )
        )
        inserted_cells += 1

    report = compute_composite_confidence(
        ocr_confidence=table.ocr_confidence,
        table_structure_confidence=table.structural_confidence,
        schema_mapping_confidence=0.9 if table_type != TableType.UNKNOWN else 0.3,
    )
    session.add(
        ExtractionValidationReport(
            document_id=document.id,
            page_number=table.page_number,
            ocr_confidence=report.ocr_confidence,
            table_structure_confidence=report.table_structure_confidence,
            schema_mapping_confidence=report.schema_mapping_confidence,
            composite_confidence=report.composite_confidence,
            passed=report.passed,
            details={
                **report.details,
                "inserted_cells": inserted_cells,
                "missing_bbox_cells": missing_bbox_cells,
                "low_bbox_confidence_cells": low_bbox_confidence_cells,
            },
        )
    )
    return extracted_table, inserted_cells, missing_bbox_cells, low_bbox_confidence_cells


def process_document(
    file_path: Path,
    page_limit: int | None = None,
    page_start: int = 1,
    page_end: int | None = None,
    skip_document_ai: bool = False,
) -> uuid.UUID:
    settings = get_settings()
    settings.data_processed_dir.mkdir(parents=True, exist_ok=True)
    settings.data_raw_dir.mkdir(parents=True, exist_ok=True)

    loaded = load_pdf(
        file_path,
        page_limit=page_limit,
        page_start=page_start,
        page_end=page_end,
        render_dpi=settings.ocr_dpi,
    )
    effective_end = page_end or (loaded.pages[-1].page_number if loaded.pages else page_start)
    processing_hash = _processing_content_hash(loaded.content_hash, page_start, effective_end)

    subset_path = settings.data_processed_dir / f"{loaded.content_hash[:12]}_{page_start}-{effective_end}.pdf"
    _write_page_subset(
        file_path,
        subset_path,
        page_start=page_start,
        page_end=page_end,
        page_limit=page_limit,
    )

    parsed: DocumentAIExtractionResult | None = None
    raw_json: dict = {}
    if not skip_document_ai:
        processor = DocumentAIProcessor()
        parsed, raw_json = processor.process_pdf(subset_path)
        _offset_extraction_pages(parsed, page_start)
        raw_json_path = (
            settings.data_processed_dir / f"{loaded.content_hash[:12]}_{page_start}-{effective_end}_document_ai.json"
        )
        raw_json_path.write_text(json.dumps(raw_json, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("stored_raw_document_ai_json", path=str(raw_json_path))

    with session_scope() as session:
        document = _persist_document(
            session,
            filename=file_path.name,
            content_hash=processing_hash,
            page_count=loaded.page_count,
        )

        extraction_pages = {page.page_number: page for page in parsed.pages} if parsed else {}

        for page_content in loaded.pages:
            triage = classify_page(page_content)
            image_path = None
            if page_content.image_bytes:
                processed_image = preprocess_page_image(page_content.image_bytes)
                image_path_obj = settings.data_processed_dir / f"{document.id}_{page_content.page_number}.png"
                image_path_obj.write_bytes(processed_image)
                image_path = str(image_path_obj)

            extracted_page = extraction_pages.get(page_content.page_number)
            page_text = page_content.text or (extracted_page.text if extracted_page else None)

            _persist_page(
                session,
                document=document,
                page_number=page_content.page_number,
                raw_text=page_text,
                page_type=triage.page_type,
                confidence=triage.confidence,
                triage_metadata={
                    **triage.metadata,
                    "table_score": triage.table_score,
                    "formula_score": triage.formula_score,
                    "persian_ratio": triage.persian_ratio,
                    "has_domain_keywords": triage.has_domain_keywords,
                    "has_mathematical_formula": triage.has_mathematical_formula,
                },
                image_path=image_path,
                has_digital_text=triage.has_digital_text,
                ocr_json={"text_excerpt": (page_text or "")[:5000]} if parsed else None,
                layout_json=extracted_page.layout_metadata if extracted_page else None,
            )

            if triage.confidence < CONFIDENCE_THRESHOLD:
                enqueue_review(
                    session,
                    object_type="document_page",
                    object_id=uuid.uuid4(),
                    document_id=document.id,
                    issue_type=ReviewIssueType.LOW_OCR_CONFIDENCE,
                    page_number=page_content.page_number,
                    confidence=triage.confidence,
                )

        if parsed:
            total_inserted = 0
            total_missing_bbox = 0
            total_low_bbox_confidence = 0
            with GeometryResolver(file_path, document_path=str(file_path)) as geometry_resolver:
                for table in parsed.tables:
                    _, inserted, missing, low_confidence = _persist_extracted_table(
                        session,
                        document,
                        table,
                        geometry_resolver=geometry_resolver,
                        bbox_confidence_threshold=settings.bbox_confidence_threshold,
                    )
                    total_inserted += inserted
                    total_missing_bbox += missing
                    total_low_bbox_confidence += low_confidence

            logger.info(
                "table_persistence_summary",
                tables=len(parsed.tables),
                inserted_cells=total_inserted,
                missing_bbox_cells=total_missing_bbox,
                low_bbox_confidence_cells=total_low_bbox_confidence,
            )

            for page_content in loaded.pages:
                text_source = page_content.text or parsed.full_text
                for extracted_formula in extract_formulas(text_source, page_content.page_number):
                    session.add(
                        Formula(
                            document_id=document.id,
                            formula_name=extracted_formula.formula_name,
                            original_expression=extracted_formula.original_expression,
                            normalized_expression=extracted_formula.normalized_expression,
                            variables=extracted_formula.variables,
                            page_number=extracted_formula.page_number,
                        )
                    )

            if parsed.full_text.strip():
                session.add(
                    DocumentChunk(
                        document_id=document.id,
                        section="document_start",
                        content=parsed.full_text[: settings.chunk_size],
                        page_number=loaded.pages[0].page_number if loaded.pages else 1,
                        language="fa,en",
                        source_type="document_ai",
                        confidence=0.8,
                        metadata_={"chunk_index": 0, "processor_format": parsed.processor_format},
                    )
                )

        logger.info("document_processed", document_id=str(document.id), pages=loaded.page_count)
        return document.id


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Process an OHSE PDF document")
    parser.add_argument("--file", required=True, type=Path, help="Path to PDF file")
    parser.add_argument(
        "--pages",
        default=None,
        help="Page count (10) or inclusive range (46-55)",
    )
    parser.add_argument(
        "--skip-document-ai",
        action="store_true",
        help="Run triage and DB persistence without calling Document AI",
    )
    args = parser.parse_args()

    page_start, page_end, page_limit = _parse_pages_argument(args.pages)
    if page_start is None:
        page_limit = page_limit or get_settings().default_page_limit
        page_start = 1

    document_id = process_document(
        args.file,
        page_limit=page_limit,
        page_start=page_start,
        page_end=page_end,
        skip_document_ai=args.skip_document_ai,
    )
    print(f"Processed document: {document_id}")


if __name__ == "__main__":
    main()
