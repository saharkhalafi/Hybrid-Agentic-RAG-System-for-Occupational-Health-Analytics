"""Composite confidence scoring for extraction quality."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceReport:
    ocr_confidence: float | None
    table_structure_confidence: float
    schema_mapping_confidence: float
    composite_confidence: float
    passed: bool
    details: dict


def compute_composite_confidence(
    ocr_confidence: float | None,
    table_structure_confidence: float | None,
    schema_mapping_confidence: float | None,
    threshold: float = 0.65,
) -> ConfidenceReport:
    table = table_structure_confidence if table_structure_confidence is not None else 0.0
    schema = schema_mapping_confidence if schema_mapping_confidence is not None else 0.0

    if ocr_confidence is None:
        composite = (table * 0.65) + (schema * 0.35)
        weights = {"ocr": None, "table": 0.65, "schema": 0.35}
        ocr_value = None
    else:
        composite = (ocr_confidence * 0.4) + (table * 0.35) + (schema * 0.25)
        weights = {"ocr": 0.4, "table": 0.35, "schema": 0.25}
        ocr_value = ocr_confidence

    return ConfidenceReport(
        ocr_confidence=ocr_value,
        table_structure_confidence=table,
        schema_mapping_confidence=schema,
        composite_confidence=composite,
        passed=composite >= threshold,
        details={"weights": weights, "threshold": threshold, "ocr_available": ocr_confidence is not None},
    )
