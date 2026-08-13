"""Shared geometry promotion resolution with row-anchor retry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import fitz

from document_ai.geometry_cell_ordering import geometry_resolve_sort_key
from document_ai.geometry_gating import CellGeometryGate, evaluate_cell_geometry_gate
from document_ai.geometry_resolver import GeometryCellInput, GeometryResolver, is_valid_bbox
from document_ai.geometry_search import search_query_variants
from ingestion.pdf_geometry import extract_cas_numbers, normalize_match_text


class PageLookup(Protocol):
    def __call__(self, page_number: int) -> fitz.Page: ...


@dataclass
class PromotionCellInput:
    page_number: int
    table_id: str
    cell_id: str
    row_index: int
    column_index: int
    text: str
    document_ai_bbox: dict[str, Any] | None = None
    bbox_provenance: str | None = None
    input_bbox_source: str | None = None
    cell_source: str | None = None


@dataclass
class PromotionCellResult:
    page_number: int
    table_id: str
    cell_id: str
    row_index: int
    column_index: int
    cell_text: str
    normalized_text: str
    resolved_bbox: dict[str, float] | None
    bbox_source: str | None
    input_bbox_source: str | None
    match_method: str
    bbox_confidence: float
    gate: str
    persist: bool
    disposition: str
    rejection_reason: str
    search_for_hit_count: int = 0
    bbox_y_center: float | None = None
    bbox_height: float | None = None
    has_traceable_evidence: bool = False
    review_flags: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)
    cell_source: str | None = None
    resolved_pass: int = 1


def bbox_y_center(bbox: dict[str, Any] | None) -> float | None:
    if not bbox or not is_valid_bbox(bbox):
        return None
    return float(bbox["y"]) + float(bbox["height"]) / 2.0


def search_for_hit_count(page: fitz.Page, text: str) -> int:
    for candidate in search_query_variants(text):
        rects = page.search_for(candidate)
        if rects:
            return len(rects)
    return 0


def rejection_reason_for(*, text: str, gate: CellGeometryGate, match_method: str) -> str:
    del match_method
    if not text:
        return "empty_cell"
    if gate == CellGeometryGate.MISSING_BBOX:
        return "missing_bbox"
    if gate == CellGeometryGate.LOW_CONFIDENCE:
        return "low_bbox_confidence"
    return gate.value


def resolve_promotion_cell(
    resolver: GeometryResolver,
    page: fitz.Page,
    cell: PromotionCellInput,
    *,
    threshold: float,
    resolved_pass: int = 1,
) -> PromotionCellResult:
    text = cell.text.strip()
    normalized = normalize_match_text(text)

    if not text:
        gate = evaluate_cell_geometry_gate(resolved_bbox=None, bbox_confidence=0.0, threshold=threshold)
        return PromotionCellResult(
            page_number=cell.page_number,
            table_id=cell.table_id,
            cell_id=cell.cell_id,
            row_index=cell.row_index,
            column_index=cell.column_index,
            cell_text=text,
            normalized_text=normalized,
            resolved_bbox=None,
            bbox_source=None,
            input_bbox_source=cell.input_bbox_source,
            match_method="none",
            bbox_confidence=0.0,
            gate=gate.gate.value,
            persist=False,
            disposition="reject",
            rejection_reason=rejection_reason_for(text=text, gate=gate.gate, match_method="none"),
            cell_source=cell.cell_source,
            resolved_pass=resolved_pass,
        )

    result = resolver.resolve(
        GeometryCellInput(
            page_number=cell.page_number,
            cell_text=text,
            table_id=cell.table_id,
            row_index=cell.row_index,
            column_index=cell.column_index,
        ),
        document_ai_bbox=cell.document_ai_bbox,
        bbox_provenance=cell.bbox_provenance,
    )
    gate = evaluate_cell_geometry_gate(
        resolved_bbox=result.bbox,
        bbox_confidence=result.match_confidence,
        threshold=threshold,
    )
    reason = rejection_reason_for(text=text, gate=gate.gate, match_method=result.match_method)
    y_center = bbox_y_center(result.bbox)
    bbox_height = float(result.bbox["height"]) if result.bbox else None
    traceable = bool(
        gate.persist
        and result.bbox
        and result.bbox_source
        and result.match_method != "none"
        and result.source_reference.get("page_number") is not None
    )

    return PromotionCellResult(
        page_number=cell.page_number,
        table_id=cell.table_id,
        cell_id=cell.cell_id,
        row_index=cell.row_index,
        column_index=cell.column_index,
        cell_text=text,
        normalized_text=normalized,
        resolved_bbox=result.bbox,
        bbox_source=result.bbox_source,
        input_bbox_source=cell.input_bbox_source,
        match_method=result.match_method,
        bbox_confidence=result.match_confidence,
        gate=gate.gate.value,
        persist=gate.persist,
        disposition="accept" if gate.persist else "reject",
        rejection_reason=reason if not gate.persist else "none",
        search_for_hit_count=search_for_hit_count(page, text),
        bbox_y_center=y_center,
        bbox_height=bbox_height,
        has_traceable_evidence=traceable,
        cell_source=cell.cell_source,
        resolved_pass=resolved_pass,
    )


def resolve_cells_with_row_anchor_retry(
    resolver: GeometryResolver,
    cells: list[PromotionCellInput],
    *,
    threshold: float,
    page_lookup: PageLookup,
) -> dict[str, PromotionCellResult]:
    """Resolve cells in anchor-friendly order, then retry missing-bbox failures."""
    ordered = sorted(
        cells,
        key=lambda cell: geometry_resolve_sort_key(
            page_number=cell.page_number,
            table_id=cell.table_id,
            row_index=cell.row_index,
            column_index=cell.column_index,
        ),
    )
    results: dict[str, PromotionCellResult] = {}

    for cell in ordered:
        page = page_lookup(cell.page_number)
        results[cell.cell_id] = resolve_promotion_cell(
            resolver, page, cell, threshold=threshold, resolved_pass=1
        )

    for cell in ordered:
        existing = results[cell.cell_id]
        if existing.persist or existing.rejection_reason != "missing_bbox" or not cell.text.strip():
            continue
        page = page_lookup(cell.page_number)
        retried = resolve_promotion_cell(
            resolver, page, cell, threshold=threshold, resolved_pass=2
        )
        if retried.persist:
            results[cell.cell_id] = retried
            continue

        cas_numbers = extract_cas_numbers(cell.text)
        if not cas_numbers or retried.rejection_reason != "missing_bbox":
            continue
        cas_cell = PromotionCellInput(
            page_number=cell.page_number,
            table_id=cell.table_id,
            cell_id=cell.cell_id,
            row_index=cell.row_index,
            column_index=cell.column_index,
            text=f"[{cas_numbers[0]}]",
            document_ai_bbox=cell.document_ai_bbox,
            bbox_provenance=cell.bbox_provenance,
            input_bbox_source=cell.input_bbox_source,
            cell_source=cell.cell_source,
        )
        cas_retried = resolve_promotion_cell(
            resolver, page, cas_cell, threshold=threshold, resolved_pass=2
        )
        if cas_retried.persist:
            cas_retried.cell_text = cell.text.strip()
            cas_retried.normalized_text = normalize_match_text(cas_retried.cell_text)
            results[cell.cell_id] = cas_retried

    return results
