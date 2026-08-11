"""Deterministic review priority — no LLM involvement."""

from __future__ import annotations

from database.models import ReviewPriority

_CRITICAL_CODES = frozenset({
    "NUMERIC_NORMALIZATION",
    "VALUE_NOT_IN_EVIDENCE",
    "numeric_integrity",
})

_HIGH_CODES = frozenset({
    "CAS_INVALID",
    "UNIT_MISMATCH",
    "ROW_ALIGNMENT_FAILED",
    "cas_validation",
    "unit_validation",
})

_MEDIUM_CODES = frozenset({
    "COLUMN_AMBIGUOUS",
    "ambiguous_header_mapping",
    "MERGED_CELL_UNRESOLVED",
    "EXTRACTION_UNCERTAIN",
    "entity",
    "incomplete_header_mapping",
    "table_structure",
    "semantic_validation",
})

_LOW_CODES = frozenset({
    "LOW_OCR_CONFIDENCE",
    "LOW_BBOX_CONFIDENCE",
    "MISSING_BBOX",
    "HEADER_UNKNOWN",
    "ocr_uncertain",
})


def compute_priority(issue_codes: list[str], *, severities: list[str] | None = None) -> ReviewPriority:
    normalized = {c.upper().replace("-", "_") for c in issue_codes}
    normalized |= {c.lower() for c in issue_codes}

    if normalized & _CRITICAL_CODES or (severities and "critical" in severities):
        return ReviewPriority.CRITICAL
    if normalized & _HIGH_CODES or (severities and "high" in severities):
        return ReviewPriority.HIGH
    if normalized & _MEDIUM_CODES or (severities and "medium" in severities):
        return ReviewPriority.MEDIUM
    if normalized & _LOW_CODES:
        return ReviewPriority.LOW
    if severities and "error" in severities:
        return ReviewPriority.HIGH
    return ReviewPriority.MEDIUM


def priority_rank(priority: ReviewPriority) -> int:
    return {
        ReviewPriority.CRITICAL: 0,
        ReviewPriority.HIGH: 1,
        ReviewPriority.MEDIUM: 2,
        ReviewPriority.LOW: 3,
    }[priority]
