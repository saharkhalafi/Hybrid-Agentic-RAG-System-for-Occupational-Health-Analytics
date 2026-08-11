
"""Resolve Document AI cell geometry against digital PDF geometry.

Document AI:
    Provides table structure and cell text.

PyMuPDF:
    Provides physical coordinates for digital PDF pages.

This module combines both sources and NEVER crashes when a cell
cannot be geometrically resolved. Unresolved cells are explicitly
marked for review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from document_ai.schema import (
    DocumentAIExtractionResult,
    ExtractionCell,
)
from ingestion.pdf_geometry import GeometryMatch, GeometryResolver
from config.logging import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

class PdfGeometryResolver:
    """Resolve Document AI extracted cells to PDF coordinates."""

    def __init__(
        self,
        pdf_path: str | Path,
        *,
        fuzzy_threshold: float = 0.85,
    ) -> None:

        self.pdf_path = Path(pdf_path)
        self.fuzzy_threshold = fuzzy_threshold

        if not self.pdf_path.exists():
            raise FileNotFoundError(
                f"PDF not found: {self.pdf_path}"
            )

        self.document = fitz.open(self.pdf_path)

        self._page_resolvers: dict[int, GeometryResolver] = {}

    # ------------------------------------------------------------------
    # Page resolver
    # ------------------------------------------------------------------

    def _get_page_resolver(
        self,
        page_number: int,
    ) -> GeometryResolver | None:

        if page_number < 1:
            return None

        if page_number > len(self.document):
            logger.warning(
                "pdf_page_out_of_range",
                page_number=page_number,
                total_pages=len(self.document),
            )
            return None

        if page_number not in self._page_resolvers:

            page = self.document[page_number - 1]

            self._page_resolvers[page_number] = GeometryResolver(
                page,
                fuzzy_threshold=self.fuzzy_threshold,
            )

        return self._page_resolvers[page_number]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        extraction: DocumentAIExtractionResult,
    ) -> GeometryResolutionReport:

        results: list[ResolvedCellGeometry] = []

        for page in extraction.pages:

            resolver = self._get_page_resolver(
                page.page_number
            )

            if resolver is None:

                for table in page.tables:
                    for row in table.rows:
                        for cell in row.cells:

                            results.append(
                                self._unresolved_cell(
                                    cell=cell,
                                    page_number=page.page_number,
                                    reason="page_unavailable",
                                )
                            )

                continue

            for table in page.tables:

                for row in table.rows:

                    for cell in row.cells:

                        result = self._resolve_cell(
                            cell=cell,
                            resolver=resolver,
                            page_number=page.page_number,
                        )

                        results.append(result)

        return self._build_report(results)

    # ------------------------------------------------------------------
    # Cell resolution
    # ------------------------------------------------------------------

    def _resolve_cell(
        self,
        *,
        cell: ExtractionCell,
        resolver: GeometryResolver,
        page_number: int,
    ) -> ResolvedCellGeometry:

        text = (cell.text or "").strip()

        # --------------------------------------------------------------
        # Empty cells
        # --------------------------------------------------------------

        if not text:

            return self._unresolved_cell(
                cell=cell,
                page_number=page_number,
                reason="empty_cell",
            )

        # --------------------------------------------------------------
        # Document AI bbox first
        # --------------------------------------------------------------

        if cell.bbox:

            confidence = (
                cell.confidence
                if cell.confidence is not None
                else 1.0
            )

            return ResolvedCellGeometry(
                page_number=page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                text=text,
                bbox=cell.bbox,
                bbox_source="document_ai",
                bbox_confidence=confidence,
                match_method="document_ai",
                matched_text=text,
                resolved=True,
                needs_review=False,
                reason=None,
            )

        # --------------------------------------------------------------
        # PyMuPDF matching
        # --------------------------------------------------------------

        try:
            match: GeometryMatch | None = resolver.resolve(text)

        except Exception as exc:

            logger.exception(
                "geometry_resolution_failed",
                page_number=page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                text=text[:200],
                error=str(exc),
            )

            return self._unresolved_cell(
                cell=cell,
                page_number=page_number,
                reason="resolver_exception",
            )

        # --------------------------------------------------------------
        # IMPORTANT:
        # resolver.resolve() is allowed to return None.
        # Never dereference None.
        # --------------------------------------------------------------

        if match is None:

            logger.warning(
                "geometry_not_resolved",
                page_number=page_number,
                row_index=cell.row_index,
                column_index=cell.column_index,
                text=text[:200],
            )

            return self._unresolved_cell(
                cell=cell,
                page_number=page_number,
                reason="no_match",
            )

        # --------------------------------------------------------------
        # Successful match
        # --------------------------------------------------------------

        return ResolvedCellGeometry(
            page_number=page_number,
            row_index=cell.row_index,
            column_index=cell.column_index,
            text=text,
            bbox=match.bbox,
            bbox_source=match.source,
            bbox_confidence=match.confidence,
            match_method=match.method,
            matched_text=match.matched_text,
            resolved=True,
            needs_review=(
                match.confidence < self.fuzzy_threshold
            ),
            reason=None,
        )

    # ------------------------------------------------------------------
    # Unresolved cell
    # ------------------------------------------------------------------

    @staticmethod
    def _unresolved_cell(
        *,
        cell: ExtractionCell,
        page_number: int,
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

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    @staticmethod
    def _build_report(
        results: list[ResolvedCellGeometry],
    ) -> GeometryResolutionReport:

        total = len(results)

        resolved = sum(
            1
            for result in results
            if result.resolved and result.bbox is not None
        )

        unresolved = total - resolved

        coverage = (
            resolved / total
            if total
            else 1.0
        )

        exact = sum(
            1
            for result in results
            if result.match_method == "exact_line"
        )

        token = sum(
            1
            for result in results
            if result.match_method == "ordered_token_sequence"
        )

        cas = sum(
            1
            for result in results
            if result.match_method in {
                "cas_match",
                "cas_line_match",
            }
        )

        fuzzy = sum(
            1
            for result in results
            if result.match_method == "fuzzy_line"
        )

        review = sum(
            1
            for result in results
            if result.needs_review
        )

        return GeometryResolutionReport(
            total_cells=total,
            resolved_cells=resolved,
            unresolved_cells=unresolved,
            coverage=coverage,
            exact_matches=exact,
            token_matches=token,
            cas_matches=cas,
            fuzzy_matches=fuzzy,
            review_cells=review,
            results=results,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying PDF."""

        if self.document is not None:
            self.document.close()

    def __enter__(self) -> "PdfGeometryResolver":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc_value: Any,
        traceback: Any,
    ) -> None:
        self.close()

