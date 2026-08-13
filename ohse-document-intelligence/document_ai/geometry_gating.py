"""Shared geometry validity and confidence gating for table-cell persistence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.settings import get_settings
from document_ai.geometry_resolver import is_valid_bbox


class CellGeometryGate(str, Enum):
    ACCEPT = "accept"
    MISSING_BBOX = "missing_bbox"
    LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class CellGeometryGateResult:
    gate: CellGeometryGate
    persist: bool


def evaluate_cell_geometry_gate(
    *,
    resolved_bbox: dict | None,
    bbox_confidence: float | None,
    threshold: float | None = None,
) -> CellGeometryGateResult:
    """Apply the same bbox validity/confidence rules used at persistence time."""
    if threshold is None:
        threshold = get_settings().bbox_confidence_threshold

    confidence = bbox_confidence if bbox_confidence is not None else 0.0

    if not is_valid_bbox(resolved_bbox):
        return CellGeometryGateResult(CellGeometryGate.MISSING_BBOX, persist=False)

    if confidence < threshold:
        return CellGeometryGateResult(CellGeometryGate.LOW_CONFIDENCE, persist=False)

    return CellGeometryGateResult(CellGeometryGate.ACCEPT, persist=True)
