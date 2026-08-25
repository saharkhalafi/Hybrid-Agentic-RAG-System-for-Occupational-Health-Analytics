"""Authoritative structured OEL row validation."""

from __future__ import annotations

from typing import Any

CANONICAL_GOLD_ARTIFACT_PATH = "canonical_evidence_v1"
ACCEPTED_VALIDATION_STATUS = "accepted"


class StructuredAuthorityError(ValueError):
    """Raised when a structured row fails canonical authority checks."""


def validate_authoritative_oel_row(
    row: dict[str, Any],
    *,
    expected_chemical_id: str | None = None,
    expected_cas: str | None = None,
) -> None:
    """Ensure structured lookup returned canonical evidence for the resolved entity."""
    if row.get("validation_status") != ACCEPTED_VALIDATION_STATUS:
        raise StructuredAuthorityError(
            f"validation_status={row.get('validation_status')!r} expected={ACCEPTED_VALIDATION_STATUS!r}"
        )
    if row.get("gold_artifact_path") != CANONICAL_GOLD_ARTIFACT_PATH:
        raise StructuredAuthorityError(
            f"gold_artifact_path={row.get('gold_artifact_path')!r} expected={CANONICAL_GOLD_ARTIFACT_PATH!r}"
        )
    if expected_chemical_id and row.get("chemical_id") != expected_chemical_id:
        raise StructuredAuthorityError(
            f"chemical_id={row.get('chemical_id')!r} expected={expected_chemical_id!r}"
        )
    if expected_cas and row.get("cas") != expected_cas:
        raise StructuredAuthorityError(
            f"cas={row.get('cas')!r} expected={expected_cas!r}"
        )
