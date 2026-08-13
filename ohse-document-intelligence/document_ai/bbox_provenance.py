"""Bbox provenance — distinguish trusted Document AI geometry from recovery."""

from __future__ import annotations

from typing import Any

from document_ai.geometry_resolver import (
    BBOX_SOURCE_DOCUMENT_AI,
    BBOX_SOURCE_OCR_OVERLAY,
    BBOX_SOURCE_PYMUPDF,
    is_valid_bbox,
)

# Explicit provenance labels for validated_structure / evidence cells.
BBOX_PROVENANCE_DOCUMENT_AI = BBOX_SOURCE_DOCUMENT_AI
BBOX_PROVENANCE_PYMUPDF_RECOVERY = "pymupdf_recovery"
BBOX_PROVENANCE_PYMUPDF_ALIGNED = "pymupdf_aligned"
BBOX_PROVENANCE_OCR_OVERLAY = BBOX_SOURCE_OCR_OVERLAY

TRUSTED_DOCUMENT_AI_BBOX_SOURCES = frozenset({BBOX_PROVENANCE_DOCUMENT_AI})

RECOVERY_BBOX_SOURCES = frozenset(
    {
        BBOX_PROVENANCE_PYMUPDF_RECOVERY,
        BBOX_PROVENANCE_PYMUPDF_ALIGNED,
        BBOX_SOURCE_PYMUPDF,
    }
)


def is_trusted_document_ai_bbox(
    bbox: dict[str, Any] | None,
    bbox_source: str | None,
) -> bool:
    return bool(bbox_source in TRUSTED_DOCUMENT_AI_BBOX_SOURCES and is_valid_bbox(bbox))


def resolve_bbox_inputs(
    bbox: dict[str, Any] | None,
    bbox_source: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (document_ai_bbox, bbox_provenance) for GeometryResolver.resolve()."""
    if is_trusted_document_ai_bbox(bbox, bbox_source):
        return bbox, BBOX_PROVENANCE_DOCUMENT_AI
    return None, None


def resolve_bbox_inputs_from_cell(cell: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Accept ExtractedCellRecord or dict-shaped validated_structure cell."""
    if hasattr(cell, "bbox"):
        bbox = cell.bbox
        bbox_source = getattr(cell, "bbox_source", None)
    else:
        bbox = cell.get("bbox")
        bbox_source = cell.get("bbox_source")
    return resolve_bbox_inputs(bbox, bbox_source)
