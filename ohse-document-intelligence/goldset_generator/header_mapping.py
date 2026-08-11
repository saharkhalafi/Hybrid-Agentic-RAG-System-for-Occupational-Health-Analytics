"""Header mapping analysis — detect ambiguity before silent acceptance."""

from __future__ import annotations

from collections import Counter
from typing import Any


def analyze_header_mapping(
    header_mapping: dict[int, str],
    *,
    physical_column_count: int | None = None,
    structure_issues: list[dict[str, Any]] | None = None,
    expected_columns: int = 7,
) -> dict[str, Any]:
    """Return mapping metadata including confidence and issues."""
    if not header_mapping:
        return {
            "header_mapping": {},
            "mapping_confidence": 0.0,
            "mapping_status": "review_required",
            "mapping_issues": [{"type": "header_unknown", "severity": "high", "message": "empty header mapping"}],
        }

    field_counts = Counter(header_mapping.values())
    duplicates = {field: count for field, count in field_counts.items() if count > 1}
    issues: list[dict[str, Any]] = list(structure_issues or [])

    if duplicates:
        issues.append(
            {
                "type": "ambiguous_header_mapping",
                "severity": "medium",
                "message": f"multiple columns mapped to same field: {duplicates}",
                "fields": duplicates,
            }
        )

    if physical_column_count is not None and physical_column_count != expected_columns:
        issues.append(
            {
                "type": "column_count_mismatch",
                "severity": "high",
                "message": f"expected {expected_columns} physical columns, detected {physical_column_count}",
                "expected": expected_columns,
                "detected": physical_column_count,
            }
        )

    if len(header_mapping) < 4:
        issues.append(
            {
                "type": "incomplete_header_mapping",
                "severity": "medium",
                "message": f"only {len(header_mapping)} columns mapped",
            }
        )

    if issues:
        status = "review_required"
        confidence = max(0.45, 0.85 - 0.12 * len(issues))
    else:
        status = "accepted"
        confidence = 0.95

    return {
        "header_mapping": {str(k): v for k, v in header_mapping.items()},
        "mapping_confidence": round(confidence, 2),
        "mapping_status": status,
        "mapping_issues": issues,
        "physical_column_count": physical_column_count,
    }
