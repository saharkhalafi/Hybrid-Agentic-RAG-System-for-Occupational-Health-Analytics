"""Process-wide cache of accepted chemical_registry rows."""

from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import ChemicalRegistry

_lock = threading.Lock()
_cached_rows: list[ChemicalRegistry] | None = None


def get_accepted_chemicals(session: Session) -> list[ChemicalRegistry]:
    """Load accepted chemicals once per process — avoids per-request full-table scans."""
    global _cached_rows
    if _cached_rows is not None:
        return _cached_rows
    with _lock:
        if _cached_rows is None:
            _cached_rows = list(
                session.scalars(
                    select(ChemicalRegistry).where(ChemicalRegistry.validation_status == "accepted")
                ).all()
            )
    return _cached_rows


def reset_chemical_registry_cache() -> None:
    """Test helper — clear the in-process registry cache."""
    global _cached_rows
    with _lock:
        _cached_rows = None
