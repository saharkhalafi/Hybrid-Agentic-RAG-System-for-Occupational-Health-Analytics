"""Shared bbox and text extraction helpers for Document AI adapters."""

from __future__ import annotations

from typing import Any


def extract_bbox_from_layout(layout: dict[str, Any] | None) -> dict[str, Any] | None:
    if not layout:
        return None

    bounding_poly = layout.get("boundingPoly") or layout.get("bounding_poly")
    if bounding_poly:
        return {"bounding_poly": bounding_poly}

    normalized_vertices = layout.get("normalizedVertices") or layout.get("normalized_vertices")
    if normalized_vertices:
        return {"normalized_vertices": normalized_vertices}

    bounding_box = layout.get("boundingBox") or layout.get("bounding_box")
    if bounding_box:
        return {"bounding_box": bounding_box}

    return None


def extract_bbox_from_block(block: dict[str, Any] | None) -> dict[str, Any] | None:
    if not block:
        return None

    for key in ("layout", "boundingBox", "bounding_box"):
        if key in block:
            bbox = extract_bbox_from_layout(block[key]) if key == "layout" else {"bounding_box": block[key]}
            if bbox:
                return bbox

    return None


def extract_text_from_anchor(full_text: str, text_anchor: dict[str, Any] | None) -> str:
    if not text_anchor:
        return ""
    segments = text_anchor.get("textSegments") or text_anchor.get("text_segments") or []
    parts: list[str] = []
    for segment in segments:
        start = int(segment.get("startIndex") or segment.get("start_index") or 0)
        end = int(segment.get("endIndex") or segment.get("end_index") or 0)
        parts.append(full_text[start:end])
    return "".join(parts).strip()


def extract_confidence_from_layout(layout: dict[str, Any] | None) -> float | None:
    if not layout:
        return None
    confidence = layout.get("confidence")
    if confidence is None:
        return None
    try:
        return float(confidence)
    except (TypeError, ValueError):
        return None


def page_span_reference(page_span: dict[str, Any] | None) -> dict[str, Any] | None:
    """Page reference is NOT a bbox — returned for metadata only."""
    if not page_span:
        return None
    page_start = page_span.get("pageStart") or page_span.get("page_start")
    page_end = page_span.get("pageEnd") or page_span.get("page_end")
    if page_start is None:
        return None
    return {"page_start": page_start, "page_end": page_end or page_start}
