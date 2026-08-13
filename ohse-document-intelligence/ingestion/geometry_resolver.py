"""Backwards-compatible batch wrapper over ``document_ai.geometry_resolver``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from document_ai.bbox_provenance import resolve_bbox_inputs
from document_ai.geometry_resolver import (
    BBOX_SOURCE_DOCUMENT_AI,
    GeometryCellInput,
    GeometryMatchResult,
    GeometryResolver,
    MATCH_CAS,
    MATCH_EXACT,
    MATCH_LINE,
    MATCH_PARTIAL,
    MATCH_TOKEN,
    MATCH_TOKENS,
)
from document_ai.schema import DocumentAIExtractionResult, ExtractionCell
from config.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedCellGeometry:
    """Final geometry resolution for one extracted cell."""

    page_number: int
    row_index: int
    column_index: int

    text: str

    bbox: dict[str, float] | None

    bbox_source: str | None
    bbox_confidence: float | None

    match_method: str | None
    matched_text: str | None

    resolved: bool
    needs_review: bool

    reason: str | None = None


@dataclass(frozen=True)
class GeometryResolutionReport:
    """Summary of geometry resolution."""

    total_cells: int
    resolved_cells: int
    unresolved_cells: int

    coverage: float

    exact_matches: int
    token_matches: int
    cas_matches: int
    fuzzy_matches: int

    review_cells: int

    results: list[ResolvedCellGeometry]


class PdfGeometryResolver:
    """Resolve Document AI extracted cells to PDF coordinates via the canonical resolver."""

    def __init__(
        self,
        pdf_path: str | Path,
        *,
        fuzzy_threshold: float = 0.85,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.fuzzy_threshold = fuzzy_threshold
        self._resolver = GeometryResolver(self.pdf_path, document_path=str(self.pdf_path))

    def close(self) -> None:
        self._resolver.close()

    def __enter__(self) -> PdfGeometryResolver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def resolve(self, extraction: DocumentAIExtractionResult) -> GeometryResolutionReport:
        results: list[ResolvedCellGeometry] = []

        for page in extraction.pages:
            for table in page.tables:
                for row in table.rows:
                    for cell in row.cells:
                        results.append(self._resolve_cell(cell, page.page_number))

        return self._build_report(results)

    def _resolve_cell(self, cell: ExtractionCell, page_number: int) -> ResolvedCellGeometry:
        text = (cell.text or "").strip()

        if not text:
            return self._unresolved_cell(cell, page_number, reason="empty_cell")

        try:
            da_bbox, bbox_provenance = resolve_bbox_inputs(cell.bbox, BBOX_SOURCE_DOCUMENT_AI if cell.bbox else None)
            match: GeometryMatchResult = self._resolver.resolve(
                GeometryCellInput(
                    page_number=cell.page_number or page_number,
                    cell_text=text,
                    table_id=table_id_from_cell(cell),
                    row_index=cell.row_index,
                    column_index=cell.column_index,
                ),
                document_ai_bbox=da_bbox,
                bbox_provenance=bbox_provenance,
            )
        except Exception as exc:
            logger.exception(
                "geometry_resolution_failed",
                page_number=page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                text=text[:200],
                error=str(exc),
            )
            return self._unresolved_cell(cell, page_number, reason="resolver_exception")

        if match.bbox is None:
            logger.warning(
                "geometry_not_resolved",
                page_number=page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                text=text[:200],
            )
            return self._unresolved_cell(cell, page_number, reason="no_match")

        return ResolvedCellGeometry(
            page_number=page_number,
            row_index=cell.row_index,
            column_index=cell.column_index,
            text=text,
            bbox=match.bbox,
            bbox_source=match.bbox_source,
            bbox_confidence=match.match_confidence,
            match_method=match.match_method,
            matched_text=match.source_reference.get("matched_text", text),
            resolved=True,
            needs_review=match.match_confidence < self.fuzzy_threshold,
            reason=None,
        )

    @staticmethod
    def _unresolved_cell(
        cell: ExtractionCell,
        page_number: int,
        *,
        reason: str,
    ) -> ResolvedCellGeometry:
        return ResolvedCellGeometry(
            page_number=page_number,
            row_index=cell.row_index,
            column_index=cell.column_index,
            text=(cell.text or "").strip(),
            bbox=None,
            bbox_source=None,
            bbox_confidence=None,
            match_method=None,
            matched_text=None,
            resolved=False,
            needs_review=True,
            reason=reason,
        )

    @staticmethod
    def _build_report(results: list[ResolvedCellGeometry]) -> GeometryResolutionReport:
        total = len(results)
        resolved = sum(1 for result in results if result.resolved and result.bbox is not None)
        unresolved = total - resolved
        coverage = resolved / total if total else 1.0

        exact_matches = sum(
            1
            for result in results
            if result.match_method == MATCH_EXACT
            or (
                result.bbox_source == BBOX_SOURCE_DOCUMENT_AI
                and result.match_method in {MATCH_EXACT, MATCH_LINE}
            )
        )
        token_matches = sum(
            1 for result in results if result.match_method in {MATCH_TOKENS, MATCH_PARTIAL}
        )
        cas_matches = sum(1 for result in results if result.match_method == MATCH_CAS)
        fuzzy_matches = sum(
            1
            for result in results
            if result.match_method in {MATCH_TOKEN, MATCH_LINE}
            and (result.bbox_confidence or 0.0) < 0.98
        )
        review_cells = sum(1 for result in results if result.needs_review)

        return GeometryResolutionReport(
            total_cells=total,
            resolved_cells=resolved,
            unresolved_cells=unresolved,
            coverage=coverage,
            exact_matches=exact_matches,
            token_matches=token_matches,
            cas_matches=cas_matches,
            fuzzy_matches=fuzzy_matches,
            review_cells=review_cells,
            results=results,
        )


def table_id_from_cell(cell: ExtractionCell) -> str:
    page = cell.page_number or 0
    return f"page-{page}-row-{cell.row_index}-col-{cell.column_index}"
