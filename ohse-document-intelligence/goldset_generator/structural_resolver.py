"""Layer 2 — deterministic structural resolver for table reconstruction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from config.logging import get_logger
from config.settings import get_settings
from document_ai.bbox_provenance import (
    BBOX_PROVENANCE_DOCUMENT_AI,
    BBOX_PROVENANCE_PYMUPDF_ALIGNED,
    BBOX_PROVENANCE_PYMUPDF_RECOVERY,
    resolve_bbox_inputs,
)
from document_ai.geometry_cell_ordering import geometry_resolve_sort_key
from document_ai.geometry_resolver import GeometryCellInput, GeometryResolver, is_valid_bbox
from goldset_generator.document_processor import (
    ExtractedCellRecord,
    ExtractedTableRecord,
    PageTextRecord,
    ProcessedDocument,
    _cell_id,
    _table_id,
)
from goldset_generator.row_visual_band import refine_table_visual_rows
from goldset_generator.table_detection_gate import evaluate_table_detection, has_chemical_oel_signatures
from ingestion.table_recovery import recover_tables_for_page
from knowledge.table_classifier import classify_table_text
from normalization.persian_normalizer import normalize_persian_text
from validation.structural_confidence import compute_structural_confidence

logger = get_logger(__name__)

CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")
MULTI_CHEMICAL_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\].*\[\d{2,7}-\d{2}-\d\]", re.DOTALL)
LIMIT_MARKERS = re.compile(r"\b(ppm|mg/m³|mg/m3|TWA|STEL|Ceiling)\b", re.IGNORECASE)


def _physical_column_count(table: ExtractedTableRecord) -> int:
    max_col = 0
    for row in table.rows or []:
        for cell in row:
            max_col = max(max_col, cell.column)
    return max_col + 1 if table.rows else 0


def _document_ai_table_quality_poor(page_text: str, tables: list[ExtractedTableRecord]) -> bool:
    """Detect when Document AI table structure is unusable — trigger Layer 2 recovery."""
    cas_in_page = len(CAS_PATTERN.findall(page_text or ""))
    if cas_in_page < 2:
        return False

    for table in tables:
        rows = table.rows or []
        if len(rows) < 2:
            continue

        header_cells = rows[0]
        header_cas = sum(len(CAS_PATTERN.findall(cell.text or "")) for cell in header_cells)
        if header_cas >= 2:
            return True

        data_rows = rows[1:]
        cas_in_data = sum(
            len(CAS_PATTERN.findall(cell.text or "")) for row in data_rows for cell in row
        )
        non_empty = sum(1 for row in data_rows for cell in row if (cell.text or "").strip())
        total_data_cells = sum(len(row) for row in data_rows)

        if cas_in_page >= 3 and cas_in_data < max(2, cas_in_page // 2):
            return True

        if total_data_cells and cas_in_page >= 3 and non_empty / total_data_cells < 0.25:
            return True

        if len(data_rows) < cas_in_page // 2:
            return True

        merged_like = 0
        total_cells = 0
        for cell in table.flat_cells():
            total_cells += 1
            if _is_merged_cell(cell.text):
                merged_like += 1
        if total_cells and merged_like / total_cells > 0.3:
            return True

    return False


def _needs_pymupdf_recovery(page_text: str, tables: list[ExtractedTableRecord]) -> bool:
    """Trigger geometry-first recovery for corrupt or under-columned OEL tables."""
    if _document_ai_table_quality_poor(page_text, tables):
        return True
    if not has_chemical_oel_signatures(page_text):
        return False
    for table in tables:
        if table.table_type != "chemical_oel":
            continue
        if _physical_column_count(table) < 7:
            return True
    return False


@dataclass
class StructuralResolverResult:
    tables: list[ExtractedTableRecord] = field(default_factory=list)
    cells: list[ExtractedCellRecord] = field(default_factory=list)
    page_detection: dict[int, dict[str, Any]] = field(default_factory=dict)
    merged_cell_count: int = 0
    recovered_table_count: int = 0
    document_ai_table_count: int = 0


def _is_merged_cell(text: str, row_span: int = 1, column_span: int = 1) -> bool:
    if row_span > 1 or column_span > 1:
        return True
    if not text:
        return False
    cas_count = len(CAS_PATTERN.findall(text))
    if cas_count > 1 or MULTI_CHEMICAL_PATTERN.search(text):
        return True
    has_cas = bool(CAS_PATTERN.search(text))
    has_limit = bool(LIMIT_MARKERS.search(text))
    has_english = bool(re.search(r"[A-Za-z]{4,}", text))
    return has_cas and has_limit and has_english


def _apply_aligned_bboxes(
    table: ExtractedTableRecord,
    aligned_by_id: dict[str, ExtractedCellRecord],
) -> ExtractedTableRecord:
    """Merge geometry-aligned bboxes into Document AI table cells."""
    new_rows: list[list[ExtractedCellRecord]] = []
    for row in table.rows:
        new_row: list[ExtractedCellRecord] = []
        for cell in row:
            aligned = aligned_by_id.get(cell.cell_id)
            if aligned and is_valid_bbox(aligned.bbox):
                new_row.append(aligned)
            else:
                new_row.append(cell)
        new_rows.append(new_row)
    return ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=new_rows,
        structural_confidence=table.structural_confidence,
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )


def _align_evidence_cells(
    evidence_cells: list[ExtractedCellRecord],
    pdf_path: Path,
) -> list[ExtractedCellRecord]:
    """Layer 2 geometry alignment for Document AI cells missing bbox."""
    from document_ai.geometry_promotion import PromotionCellInput, resolve_cells_with_row_anchor_retry

    inputs: list[PromotionCellInput] = []
    by_id: dict[str, ExtractedCellRecord] = {}
    for cell in evidence_cells:
        by_id[cell.cell_id] = cell
        if is_valid_bbox(cell.bbox) and cell.bbox_source == BBOX_PROVENANCE_DOCUMENT_AI:
            continue
        da_bbox, bbox_provenance = resolve_bbox_inputs(cell.bbox, cell.bbox_source)
        inputs.append(
            PromotionCellInput(
                page_number=cell.page_number,
                table_id=cell.table_id,
                cell_id=cell.cell_id,
                row_index=cell.row,
                column_index=cell.column,
                text=cell.text,
                document_ai_bbox=da_bbox,
                bbox_provenance=bbox_provenance,
                input_bbox_source=cell.bbox_source,
                cell_source=cell.source,
            )
        )

    if not inputs:
        return evidence_cells

    settings = get_settings()
    threshold = settings.bbox_confidence_threshold
    aligned: list[ExtractedCellRecord] = []
    with fitz.open(pdf_path) as doc, GeometryResolver(pdf_path, document_path=str(pdf_path)) as resolver:
        page_cache = {page_number: doc[page_number - 1] for page_number in {cell.page_number for cell in inputs}}

        def page_lookup(page_number: int) -> fitz.Page:
            return page_cache[page_number]

        resolved = resolve_cells_with_row_anchor_retry(
            resolver,
            inputs,
            threshold=threshold,
            page_lookup=page_lookup,
        )

        for cell in evidence_cells:
            if cell.cell_id not in resolved:
                aligned.append(cell)
                continue
            geometry = resolved[cell.cell_id]
            aligned_provenance = BBOX_PROVENANCE_PYMUPDF_ALIGNED
            if geometry.bbox_source == BBOX_PROVENANCE_DOCUMENT_AI:
                aligned_provenance = BBOX_PROVENANCE_DOCUMENT_AI
            aligned.append(
                ExtractedCellRecord(
                    cell_id=cell.cell_id,
                    table_id=cell.table_id,
                    page_number=cell.page_number,
                    row=cell.row,
                    column=cell.column,
                    text=cell.text,
                    bbox=geometry.resolved_bbox,
                    confidence=cell.confidence,
                    bbox_confidence=geometry.bbox_confidence,
                    bbox_source=aligned_provenance if geometry.resolved_bbox else None,
                    source=cell.source,
                    normalized_value=cell.normalized_value,
                    source_reference={
                        **cell.source_reference,
                        "bbox": geometry.resolved_bbox,
                        "bbox_source": aligned_provenance if geometry.resolved_bbox else None,
                        "alignment_layer": "structural_resolver",
                        "geometry_resolved_pass": geometry.resolved_pass,
                    },
                )
            )
    return aligned


def _recovered_to_records(recovered, table_index: int) -> tuple[ExtractedTableRecord, list[ExtractedCellRecord]]:
    page_number = recovered.page_number
    tid = _table_id(page_number, table_index)
    row_records: list[list[ExtractedCellRecord]] = []
    all_cells: list[ExtractedCellRecord] = []
    merged_count = 0

    for row in recovered.rows:
        cell_row: list[ExtractedCellRecord] = []
        for cell in row:
            merged = _is_merged_cell(cell.text)
            if merged:
                merged_count += 1
            normalized = normalize_persian_text(cell.text) if cell.text else None
            cid = _cell_id(tid, cell.row, cell.column)
            record = ExtractedCellRecord(
                cell_id=cid,
                table_id=tid,
                page_number=page_number,
                row=cell.row,
                column=cell.column,
                text=cell.text,
                bbox=cell.bbox,
                confidence=cell.confidence,
                bbox_confidence=cell.confidence if cell.bbox else None,
                bbox_source=BBOX_PROVENANCE_PYMUPDF_RECOVERY if cell.bbox else None,
                source="structural_resolver",
                normalized_value=normalized.normalized if normalized else None,
                source_reference={
                    "page_number": page_number,
                    "cell_ids": [cid],
                    "bbox": cell.bbox,
                    "recovery_method": recovered.recovery_method,
                    "column_name": cell.column_name,
                    "value_status": "merged_cell" if merged else "extracted",
                    "source_words": getattr(cell, "source_words", None) or [],
                },
            )
            cell_row.append(record)
            all_cells.append(record)
        row_records.append(cell_row)

    table_record = ExtractedTableRecord(
        table_id=tid,
        page_number=page_number,
        table_type=recovered.table_type,
        rows=row_records,
        structural_confidence=recovered.structural_confidence,
        raw_markdown=recovered.raw_markdown,
        bbox=recovered.bbox,
    )
    return table_record, all_cells, merged_count


def resolve_structure(
    processed: ProcessedDocument,
    pdf_path: Path,
) -> StructuralResolverResult:
    """Run Layer 2 structural resolution on Layer 1 evidence."""
    result = StructuralResolverResult()
    evidence_by_page: dict[int, list[ExtractedCellRecord]] = {}
    evidence_tables_by_page: dict[int, list[ExtractedTableRecord]] = {}

    for table in processed.tables:
        if table.rows and all(c.source == "document_ai" for c in table.flat_cells()):
            evidence_tables_by_page.setdefault(table.page_number, []).append(table)
            result.document_ai_table_count += 1

    for cell in processed.cells:
        if cell.source == "document_ai":
            evidence_by_page.setdefault(cell.page_number, []).append(cell)

    aligned_evidence = _align_evidence_cells(processed.cells, pdf_path) if processed.cells else []
    aligned_by_id: dict[str, ExtractedCellRecord] = {cell.cell_id: cell for cell in aligned_evidence}
    aligned_by_page: dict[int, list[ExtractedCellRecord]] = {}
    for cell in aligned_evidence:
        aligned_by_page.setdefault(cell.page_number, []).append(cell)

    table_index_by_page: dict[int, int] = {}

    for page in processed.pages:
        page_num = page.page_number
        page_evidence_tables = evidence_tables_by_page.get(page_num, [])
        detection = evaluate_table_detection(
            document_type="chemical_oel_table" if has_chemical_oel_signatures(page.text) else "unknown",
            page_text=page.text,
            document_ai_tables=[t.to_dict() for t in page_evidence_tables],
        )

        needs_recovery = (
            _needs_pymupdf_recovery(page.text, page_evidence_tables)
            if page_evidence_tables
            else bool(detection.get("needs_recovery"))
        )

        if page_evidence_tables and not needs_recovery:
            for table in page_evidence_tables:
                table = _apply_aligned_bboxes(table, aligned_by_id)
                new_rows, split_count = refine_table_visual_rows(table.table_id, table.rows)
                if split_count:
                    logger.info(
                        "visual_row_band_split",
                        page_number=page_num,
                        table_id=table.table_id,
                        split_rows=split_count,
                    )
                    table = ExtractedTableRecord(
                        table_id=table.table_id,
                        page_number=table.page_number,
                        table_type=table.table_type,
                        rows=new_rows,
                        structural_confidence=table.structural_confidence,
                        raw_markdown=table.raw_markdown,
                        bbox=table.bbox,
                    )
                merged = 0
                for cell in table.flat_cells():
                    if _is_merged_cell(cell.text):
                        merged += 1
                        cell.source_reference = {
                            **(cell.source_reference or {}),
                            "value_status": "merged_cell",
                        }
                result.merged_cell_count += merged
                result.tables.append(table)
                result.cells.extend(table.flat_cells())
            detection["table_detection_status"] = "detected"
            result.page_detection[page_num] = detection
            continue

        if needs_recovery:
            logger.info(
                "pymupdf_recovery_triggered",
                page_number=page_num,
                cas_count=len(CAS_PATTERN.findall(page.text or "")),
                document_ai_tables=len(page_evidence_tables),
            )
            detection["needs_recovery"] = True
            if page_evidence_tables:
                detection["table_detection_status"] = "under_columned_or_corrupt"
            else:
                detection["table_detection_status"] = "missed_by_document_ai"

        if not detection.get("needs_recovery"):
            result.page_detection[page_num] = detection
            continue

        recovered_list = recover_tables_for_page(pdf_path, page_num, page.text)
        if recovered_list:
            for recovered in recovered_list:
                table_index_by_page[page_num] = table_index_by_page.get(page_num, 0) + 1
                table_record, new_cells, merged = _recovered_to_records(
                    recovered, table_index_by_page[page_num]
                )
                result.merged_cell_count += merged
                result.recovered_table_count += 1
                result.tables.append(table_record)
                result.cells.extend(new_cells)
            detection = {
                **detection,
                "table_detection_status": "recovered",
                "recovery_method": recovered.recovery_method,
            }
        result.page_detection[page_num] = detection

    return result


def write_validated_structure(
    structural: StructuralResolverResult,
    processed: ProcessedDocument,
    *,
    start_page: int,
    end_page: int,
) -> Path:
    settings = get_settings()
    path = settings.data_intermediate_dir / f"validated_structure_{start_page}-{end_page}.json"
    payload = {
        "source_pdf": processed.source_pdf,
        "content_hash": processed.content_hash,
        "pages_range": {"start": start_page, "end": end_page},
        "document_ai_table_count": structural.document_ai_table_count,
        "recovered_table_count": structural.recovered_table_count,
        "merged_cell_count": structural.merged_cell_count,
        "page_detection": structural.page_detection,
        "tables": [t.to_dict() for t in structural.tables],
        "cells": [c.to_dict() for c in structural.cells],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote_validated_structure", path=str(path), tables=len(structural.tables), cells=len(structural.cells))
    return path
