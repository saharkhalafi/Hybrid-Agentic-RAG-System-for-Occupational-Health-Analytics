"""Geometry-first multi-row header reconstruction for OEL tables.

Physical column structure MUST be resolved before semantic field mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")

HEADER_KEYWORDS: dict[str, list[str]] = {
    "row_number": ["ردیف", "row", "no", "#"],
    "chemical_name": ["نام علمی", "نام", "chemical", "material", "ماده شیمیایی"],
    "molecular_weight": ["وزن ملکولی", "molecular weight", "mw"],
    "exposure_limit.stel_c": ["stel/c", "stel"],
    "exposure_limit.twa": ["twa"],
    "exposure_limit.parent": ["حد مجاز مواجهه", "exposure limit", "مواجهه شغلی"],
    "symbols": ["نماد", "symbol"],
    "exposure_basis": ["مبنای", "health", "effect", "تعیین حد"],
}

EXPECTED_OEL_PHYSICAL_COLUMNS = 7


@dataclass
class PhysicalColumn:
    physical_column_id: str
    column_index: int
    header_path: list[str]
    semantic_field: str
    parent_header: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "physical_column_id": self.physical_column_id,
            "column_index": self.column_index,
            "header_path": self.header_path,
            "semantic_field": self.semantic_field,
            "parent_header": self.parent_header,
        }


@dataclass
class HeaderStructure:
    header_row_indices: list[int]
    physical_columns: list[PhysicalColumn]
    physical_column_count: int
    data_row_start: int
    header_mapping: dict[int, str]
    issues: list[dict[str, Any]] = field(default_factory=list)
    geometry_valid: bool = True
    header_structure_valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "header_row_indices": self.header_row_indices,
            "physical_columns": [col.to_dict() for col in self.physical_columns],
            "physical_column_count": self.physical_column_count,
            "data_row_start": self.data_row_start,
            "header_mapping": {str(k): v for k, v in self.header_mapping.items()},
            "issues": self.issues,
            "geometry_valid": self.geometry_valid,
            "header_structure_valid": self.header_structure_valid,
        }


def _cell_text(cell: dict[str, Any]) -> str:
    return str(cell.get("text") or "").strip()


def _cell_col_span(cell: dict[str, Any]) -> int:
    ref = cell.get("source_reference") or {}
    return max(1, int(ref.get("column_span") or 1))


def _match_semantic(header_text: str) -> str | None:
    normalized = header_text.lower().strip()
    # Parent exposure header must win over child keyword hits inside combined text.
    if any(k in normalized for k in ("حد مجاز مواجهه", "مواجهه شغلی", "exposure limit")):
        if "stel" in normalized or "twa" in normalized:
            return "exposure_limit.parent"
    for field, keywords in HEADER_KEYWORDS.items():
        if field == "exposure_limit.parent":
            continue
        for keyword in keywords:
            if keyword in normalized:
                return field
    for keyword in HEADER_KEYWORDS["exposure_limit.parent"]:
        if keyword in normalized:
            return "exposure_limit.parent"
    return None


def _is_non_exposure_column(header_text: str) -> bool:
    matched = _match_semantic(header_text)
    return matched in {
        "molecular_weight",
        "chemical_name",
        "row_number",
        "symbols",
        "exposure_basis",
    }


def _is_header_row(row: list[dict[str, Any]], *, row_index: int) -> bool:
    texts = [_cell_text(c) for c in row if isinstance(c, dict)]
    non_empty = [t for t in texts if t]
    if not non_empty:
        return row_index == 0

    cas_count = sum(len(CAS_PATTERN.findall(t)) for t in non_empty)
    if cas_count >= 2:
        return False

    header_hits = sum(1 for t in non_empty if _match_semantic(t))
    if row_index == 0 and header_hits >= 1:
        return True

    # Sub-header row: explicit STEL/TWA child labels only.
    if row_index <= 2 and any(
        t.lower().strip() in {"stel/c", "stel", "twa"} for t in non_empty
    ):
        return True

    # Continuation rows with health-effect text are data, not headers.
    if row_index > 0 and len(non_empty) <= 2:
        return False

    return row_index == 0 and header_hits >= 2


def _is_data_row(row: list[dict[str, Any]]) -> bool:
    texts = [_cell_text(c) for c in row if isinstance(c, dict)]
    non_empty = [t for t in texts if t]
    if not non_empty:
        return False
    if any(CAS_PATTERN.search(t) for t in non_empty):
        return True
    if any(re.search(r"[\d۰-۹]+(?:[./][\d۰-۹]+)?\s*(?:ppm|mg/m|f/ml)", t, re.I) for t in non_empty):
        return True
    return False


def _detect_header_and_data_rows(rows: list[list[dict[str, Any]]]) -> tuple[list[int], int]:
    header_rows: list[int] = []
    data_start = len(rows)

    for idx, row in enumerate(rows):
        if not row:
            continue
        if _is_data_row(row):
            data_start = idx
            break
        if _is_header_row(row, row_index=idx):
            header_rows.append(idx)
        elif idx <= 2 and not _is_data_row(row):
            # Continuation/sub-header row (often empty exposure sub-columns).
            header_rows.append(idx)

    if not header_rows and rows:
        header_rows = [0]
    if data_start <= max(header_rows or [0]):
        data_start = max(header_rows or [0]) + 1
    return header_rows, data_start


def _max_column_index(rows: list[list[dict[str, Any]]]) -> int:
    max_col = 0
    for row in rows:
        for cell in row:
            if not isinstance(cell, dict):
                continue
            col = int(cell.get("column") or 0)
            span = _cell_col_span(cell)
            max_col = max(max_col, col + span - 1)
    return max_col


def _build_header_grid(
    rows: list[list[dict[str, Any]]],
    header_row_indices: list[int],
) -> dict[int, list[tuple[str, int]]]:
    """Map column_index → list of (header_text, source_row_index)."""
    grid: dict[int, list[tuple[str, int]]] = {}
    for row_idx in header_row_indices:
        if row_idx >= len(rows):
            continue
        for cell in rows[row_idx]:
            if not isinstance(cell, dict):
                continue
            col = int(cell.get("column") or 0)
            span = _cell_col_span(cell)
            text = _cell_text(cell)
            for offset in range(span):
                target = col + offset
                if text:
                    grid.setdefault(target, []).append((text, row_idx))
                else:
                    grid.setdefault(target, []).append(("", row_idx))
    return grid


def _has_exposure_sibling(
    column_index: int,
    all_headers: dict[int, list[tuple[str, int]]],
) -> bool:
    sibling = column_index + 1
    if sibling not in all_headers:
        return False
    texts = [t for t, _ in all_headers[sibling]]
    combined = " ".join(texts).lower().strip()
    if not combined:
        # Empty sibling under merged exposure parent.
        parent_text = " ".join(t for t, _ in all_headers.get(column_index, [])).lower()
        return "حد مجاز" in parent_text or ("stel" in parent_text and "twa" in parent_text)
    if _is_non_exposure_column(combined):
        return False
    return "stel" in combined or "twa" in combined or "حد مجاز" in combined


def _infer_exposure_children(
    column_index: int,
    header_parts: list[str],
    *,
    all_headers: dict[int, list[tuple[str, int]]],
) -> tuple[str, list[str]]:
    non_empty_parts = [p for p in header_parts if p.strip()]
    combined = " ".join(non_empty_parts).lower()
    parent = next((p for p in non_empty_parts if "حد مجاز" in p or "exposure" in p.lower()), None)

    # Empty child column under a merged exposure parent → second limit column (TWA).
    if not combined.strip() and column_index > 0:
        prev_parts = [t for t, _ in all_headers.get(column_index - 1, []) if t.strip()]
        prev_combined = " ".join(prev_parts).lower()
        if "حد مجاز" in prev_combined and "stel" in prev_combined and "twa" in prev_combined:
            return "exposure_limit.twa", [prev_parts[0], "TWA"]

    # Explicit child header labels from sub-header row.
    if combined.strip() in {"stel/c", "stel"} or "stel/c" in combined and "twa" not in combined:
        return "exposure_limit.stel_c", header_parts
    if combined.strip() == "twa" or ("twa" in combined and "stel" not in combined):
        return "exposure_limit.twa", header_parts

    has_stel = "stel" in combined
    has_twa = "twa" in combined
    sibling = _has_exposure_sibling(column_index, all_headers)

    if has_stel and has_twa and sibling:
        return "exposure_limit.stel_c", [parent or "حد مجاز مواجهه شغلی", "STEL/C"]

    if has_stel and has_twa and not sibling:
        return "TWA_STEL", non_empty_parts

    if has_stel:
        return "exposure_limit.stel_c", [parent or "حد مجاز مواجهه شغلی", "STEL/C"]
    if has_twa:
        return "exposure_limit.twa", [parent or "حد مجاز مواجهه شغلی", "TWA"]

    return f"column_{column_index}", non_empty_parts or [f"column_{column_index}"]


def _semantic_field_for_column(
    column_index: int,
    header_parts: list[str],
    *,
    all_headers: dict[int, list[tuple[str, int]]],
) -> tuple[str, list[str]]:
    # Prefer explicit child labels from the deepest header row.
    for part in reversed(header_parts):
        lower = part.lower().strip()
        if lower in {"stel/c", "stel"}:
            parent = next((p for p in header_parts if "حد مجاز" in p), header_parts[0] if header_parts else "")
            return "exposure_limit.stel_c", [parent, "STEL/C"]
        if lower == "twa":
            parent = next((p for p in header_parts if "حد مجاز" in p), header_parts[0] if header_parts else "")
            return "exposure_limit.twa", [parent, "TWA"]

    combined = " ".join(header_parts)
    matched = _match_semantic(combined)
    if matched == "exposure_limit.parent":
        return _infer_exposure_children(column_index, header_parts, all_headers=all_headers)
    if matched:
        label = header_parts[-1] if header_parts else combined
        return matched, header_parts or [label]

    if not any(p.strip() for p in header_parts) and column_index > 0:
        return _infer_exposure_children(column_index, header_parts, all_headers=all_headers)

    if not any(p.strip() for p in header_parts):
        return f"column_{column_index}", [f"column_{column_index}"]

    prev_parts = [t for t, _ in all_headers.get(column_index - 1, [])]
    if prev_parts and any("حد مجاز" in p or "stel" in p.lower() or "twa" in p.lower() for p in prev_parts if p.strip()):
        return _infer_exposure_children(column_index, prev_parts + header_parts, all_headers=all_headers)

    return f"column_{column_index}", header_parts or [f"column_{column_index}"]


def _legacy_field_name(semantic: str) -> str:
    mapping = {
        "exposure_limit.stel_c": "STEL",
        "exposure_limit.twa": "TWA",
        "exposure_limit.parent": "TWA_STEL",
        "exposure_basis": "health_effect",
        "row_number": "row_number",
        "chemical_name": "chemical_name",
        "molecular_weight": "molecular_weight",
        "symbols": "symbols",
        "TWA_STEL": "TWA_STEL",
    }
    return mapping.get(semantic, semantic)


_RECOVERY_COLUMN_HINTS = {
    "health_effect": "exposure_basis",
    "STEL": "exposure_limit.stel_c",
    "TWA": "exposure_limit.twa",
}


def _column_name_hints(rows: list[list[dict[str, Any]]]) -> dict[int, str]:
    """Use geometry-recovery column_name metadata when present."""
    hints: dict[int, str] = {}
    for row in rows:
        for cell in row:
            if not isinstance(cell, dict):
                continue
            col_name = (cell.get("source_reference") or {}).get("column_name")
            if not col_name:
                continue
            col_idx = int(cell.get("column") or 0)
            if col_idx not in hints:
                hints[col_idx] = col_name
    return hints


def _apply_recovery_hints(
    physical_columns: list[PhysicalColumn],
    hints: dict[int, str],
) -> list[PhysicalColumn]:
    if not hints:
        return physical_columns
    updated: list[PhysicalColumn] = []
    for col in physical_columns:
        hint = hints.get(col.column_index)
        if not hint:
            updated.append(col)
            continue
        semantic = _RECOVERY_COLUMN_HINTS.get(hint, hint)
        if semantic in {"health_effect", "STEL", "TWA", "molecular_weight", "chemical_name", "row_number", "symbols"}:
            legacy = _legacy_field_name(semantic if semantic != hint else hint)
            if hint == "health_effect":
                legacy = "health_effect"
            path = col.header_path
            if hint in {"STEL", "TWA"}:
                parent = next((p for p in path if "حد مجاز" in p), "حد مجاز مواجهه شغلی")
                path = [parent, hint if hint != "STEL" else "STEL/C"]
            updated.append(
                PhysicalColumn(
                    physical_column_id=col.physical_column_id,
                    column_index=col.column_index,
                    header_path=path,
                    semantic_field=semantic if semantic.startswith("exposure_limit") else hint,
                    parent_header=path[0] if len(path) > 1 else col.parent_header,
                )
            )
        else:
            updated.append(col)
    return updated


def reconstruct_header_structure(
    rows: list[list[dict[str, Any]]],
    *,
    table_type: str = "chemical_oel",
    expected_columns: int = EXPECTED_OEL_PHYSICAL_COLUMNS,
) -> HeaderStructure:
    """Build physical column model from table rows before semantic extraction."""
    issues: list[dict[str, Any]] = []

    if not rows:
        return HeaderStructure(
            header_row_indices=[],
            physical_columns=[],
            physical_column_count=0,
            data_row_start=0,
            header_mapping={},
            issues=[{"type": "empty_table", "severity": "critical", "message": "no rows"}],
            geometry_valid=False,
            header_structure_valid=False,
        )

    header_row_indices, data_row_start = _detect_header_and_data_rows(rows)
    max_col = _max_column_index(rows)
    physical_column_count = max_col + 1

    # Contaminated header detection: header row contains multiple CAS numbers.
    for h_idx in header_row_indices:
        row = rows[h_idx]
        cas_in_header = sum(len(CAS_PATTERN.findall(_cell_text(c))) for c in row if isinstance(c, dict))
        if cas_in_header >= 2:
            issues.append(
                {
                    "type": "contaminated_header_row",
                    "severity": "critical",
                    "message": f"header row {h_idx} contains {cas_in_header} CAS tokens — structure corrupt",
                    "row_index": h_idx,
                }
            )

    header_grid = _build_header_grid(rows, header_row_indices)
    physical_columns: list[PhysicalColumn] = []

    for col_idx in range(physical_column_count):
        parts = [text for text, _ in header_grid.get(col_idx, [])]
        semantic, path = _semantic_field_for_column(col_idx, parts, all_headers=header_grid)
        parent = path[0] if len(path) > 1 else None
        physical_columns.append(
            PhysicalColumn(
                physical_column_id=f"C{col_idx}",
                column_index=col_idx,
                header_path=path,
                semantic_field=semantic,
                parent_header=parent,
            )
        )

    recovery_hints = _column_name_hints(rows)
    if recovery_hints:
        physical_columns = _apply_recovery_hints(physical_columns, recovery_hints)

    header_mapping = {
        col.column_index: _legacy_field_name(col.semantic_field)
        for col in physical_columns
        if not col.semantic_field.startswith("column_")
    }

    # Column count validation for OEL tables.
    if table_type == "chemical_oel":
        mapped_semantic = {col.semantic_field for col in physical_columns}
        required = {
            "row_number",
            "chemical_name",
            "molecular_weight",
            "symbols",
            "exposure_basis",
        }
        missing = required - mapped_semantic
        has_limits = (
            "exposure_limit.stel_c" in mapped_semantic
            or "exposure_limit.twa" in mapped_semantic
            or "TWA_STEL" in header_mapping.values()
        )
        if missing:
            issues.append(
                {
                    "type": "missing_semantic_columns",
                    "severity": "high",
                    "message": f"missing semantic columns: {sorted(missing)}",
                }
            )
        if not has_limits:
            issues.append(
                {
                    "type": "missing_exposure_columns",
                    "severity": "high",
                    "message": "no STEL/TWA columns detected",
                }
            )
        if physical_column_count != expected_columns:
            issues.append(
                {
                    "type": "column_count_mismatch",
                    "severity": "high",
                    "message": f"expected {expected_columns} physical columns, detected {physical_column_count}",
                    "expected": expected_columns,
                    "detected": physical_column_count,
                }
            )

    geometry_valid = not any(i["type"] == "contaminated_header_row" for i in issues)
    header_structure_valid = not any(
        i["type"] in {"missing_semantic_columns", "missing_exposure_columns", "column_count_mismatch"}
        for i in issues
    )

    return HeaderStructure(
        header_row_indices=header_row_indices,
        physical_columns=physical_columns,
        physical_column_count=physical_column_count,
        data_row_start=data_row_start,
        header_mapping=header_mapping,
        issues=issues,
        geometry_valid=geometry_valid,
        header_structure_valid=header_structure_valid,
    )
