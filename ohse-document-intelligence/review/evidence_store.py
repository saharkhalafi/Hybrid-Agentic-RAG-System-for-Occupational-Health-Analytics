"""Read immutable table evidence used by the HITL workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT


def find_latest_table_evidence(table_id: str) -> Path | None:
    """Return the newest evidence artifact for a stable table id.

    Multiple pipeline runs may exist under ``data/evidence/<run>/tables``.
    The bridge imports the newest gold artifact, so the newest matching
    evidence artifact is the corresponding best local source.
    """
    matches = list((PROJECT_ROOT / "data" / "evidence").glob(f"*/tables/{table_id}.json"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def load_table_evidence(table_id: str) -> dict[str, Any] | None:
    path = find_latest_table_evidence(table_id)
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def flatten_table_evidence_cells(table_id: str) -> list[dict[str, Any]]:
    table = load_table_evidence(table_id)
    if not table:
        return []
    return [
        cell
        for row in table.get("rows") or []
        for cell in row
        if isinstance(cell, dict)
    ]
