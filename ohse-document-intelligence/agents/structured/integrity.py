"""Canonical OEL data-integrity checks for structured authority."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.structured.authority import ACCEPTED_VALIDATION_STATUS, CANONICAL_GOLD_ARTIFACT_PATH
from database.models import OELChemicalLimit

LIMIT_TYPES = ("TWA", "STEL", "CEILING")


def find_duplicate_canonical_limit_types(session: Session) -> list[dict[str, Any]]:
    """Return true duplicates: same chemical_id + limit type on multiple canonical rows."""
    rows = session.scalars(
        select(OELChemicalLimit).where(
            OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
            OELChemicalLimit.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH,
        )
    ).all()

    counts: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        chemical_id = str(row.chemical_id)
        if row.twa is not None:
            counts[(chemical_id, "TWA")].append(row.source_row_key or "")
        if row.stel is not None:
            counts[(chemical_id, "STEL")].append(row.source_row_key or "")
        if row.ceiling is not None:
            counts[(chemical_id, "CEILING")].append(row.source_row_key or "")

    duplicates: list[dict[str, Any]] = []
    for (chemical_id, limit_type), keys in counts.items():
        if len(keys) <= 1:
            continue
        duplicates.append(
            {
                "chemical_id": chemical_id,
                "limit_type": limit_type,
                "count": len(keys),
                "source_row_keys": sorted(k for k in keys if k),
            }
        )
    return duplicates
