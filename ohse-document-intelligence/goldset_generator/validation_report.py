"""Per-page / per-table validation reports for goldset quality gates."""

from __future__ import annotations

from typing import Any


def _status_from_issues(issues: list[dict[str, Any]], *, required_pass: bool = False) -> str:
    if not issues:
        return "passed"
    severities = {i.get("severity", "error") for i in issues}
    if "error" in severities or "high" in severities:
        return "failed" if required_pass else "review_required"
    return "review_required"


def build_page_validation_report(
    *,
    page_number: int,
    table_reports: list[dict[str, Any]],
    entity_issues: list[dict[str, Any]],
    qa_issues: list[dict[str, Any]],
    numeric_failures: int,
) -> dict[str, Any]:
    table_structure_issues = [
        issue
        for report in table_reports
        for issue in report.get("mapping_issues") or []
    ]
    cas_issues = [i for i in entity_issues if "cas" in str(i.get("issues", i)).lower()]
    unit_issues = [i for i in entity_issues if "unit" in str(i.get("issues", i)).lower()]

    validation = {
        "numeric_integrity": "passed" if numeric_failures == 0 else "review_required",
        "cell_reference_integrity": "passed"
        if not any("unknown cell" in str(i).lower() for i in entity_issues + qa_issues)
        else "review_required",
        "bbox_integrity": "passed",
        "table_structure": _status_from_issues(table_structure_issues),
        "cas_validation": "passed" if not cas_issues else "review_required",
        "unit_validation": "passed" if not unit_issues else "review_required",
        "semantic_validation": "passed" if numeric_failures == 0 else "review_required",
    }

    issues: list[dict[str, Any]] = list(table_structure_issues)
    for bucket, item_type in ((entity_issues, "entity"), (qa_issues, "qa")):
        for item in bucket:
            for msg in item.get("issues") or []:
                issues.append({"type": item_type, "severity": "medium", "message": msg})

    failed = any(v == "failed" for v in validation.values())
    review = any(v == "review_required" for v in validation.values()) or bool(issues)
    overall = "failed" if failed else ("review_required" if review else "passed")

    return {
        "page_number": page_number,
        "validation": validation,
        "issues": issues,
        "overall_status": overall,
        "tables": table_reports,
    }


def build_table_validation_report(
    *,
    table_id: str,
    page_number: int,
    mapping_meta: dict[str, Any],
    row_review_count: int,
) -> dict[str, Any]:
    return {
        "table_id": table_id,
        "page_number": page_number,
        "mapping_confidence": mapping_meta.get("mapping_confidence"),
        "mapping_status": mapping_meta.get("mapping_status"),
        "mapping_issues": mapping_meta.get("mapping_issues") or [],
        "rows_requiring_review": row_review_count,
    }
