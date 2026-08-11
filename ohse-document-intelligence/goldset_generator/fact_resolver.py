"""Resolve fact values strictly from evidence cells and validated table gold fields."""

from __future__ import annotations

import re
from typing import Any

from goldset_generator.oel_row_parser import parse_molecular_weight

CAS_BRACKET_PATTERN = re.compile(r"\[(\d{2,7}-\d{2}-\d)\s*\]")
CAS_TOKEN_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
EXPOSURE_PATTERN = re.compile(
    r"([\d۰-۹]+(?:[./][\d۰-۹]+)?)\s*(ppm|mg/m³|mg/m3|f/ml)\b",
    re.IGNORECASE,
)
PREDICATE_FIELD_MAP: dict[str, str] = {
    "has_CAS": "CAS",
    "has_TWA": "TWA",
    "has_STEL": "STEL",
    "has_ceiling": "ceiling",
    "has_molecular_weight": "molecular_weight",
    "has_health_effect": "health_effect",
    "has_symbols": "symbols",
}


def cell_page_number(cell_id: str | None) -> int | None:
    if not cell_id:
        return None
    match = re.search(r"table_(\d{3})_", cell_id)
    return int(match.group(1)) if match else None


def resolve_cell_text(cell_by_id: dict[str, dict[str, Any]], cell_id: str | None) -> str | None:
    if not cell_id:
        return None
    cell = cell_by_id.get(cell_id)
    if not cell:
        return None
    return cell.get("text") or cell.get("normalized_value")


def resolve_field_display(field_name: str, field_data: dict[str, Any] | None) -> str | None:
    """Return the canonical display value for a validated table gold field."""
    if not field_data or not isinstance(field_data, dict):
        return None

    value = field_data.get("value")
    unit = field_data.get("unit")
    status = field_data.get("value_status")
    original = field_data.get("original_value") or ""

    if field_name == "CAS":
        if value:
            return str(value)
        bracket = CAS_BRACKET_PATTERN.search(original)
        if bracket:
            return bracket.group(1)
        token = CAS_TOKEN_PATTERN.search(original)
        return token.group(1) if token else None

    if field_name in {"TWA", "STEL", "ceiling"}:
        if value and unit:
            value_text = str(value)
            if unit.lower() in value_text.lower():
                return value_text
            return f"{value} {unit}"
        if value:
            return str(value)
        if status == "merged_cell":
            match = EXPOSURE_PATTERN.search(original)
            if match:
                return f"{match.group(1)} {match.group(2)}"
        return None

    if field_name == "molecular_weight":
        if value:
            return str(value)
        mw = parse_molecular_weight(original)
        return mw

    if value is not None and str(value).strip():
        return str(value).strip()

    if status == "merged_cell":
        return None

    return None


def resolve_object_reference(
    object_reference: dict[str, Any],
    cell_by_id: dict[str, dict[str, Any]],
    *,
    field_data: dict[str, Any] | None = None,
    field_name: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve object text from field gold first, then evidence cell."""
    if field_data and field_name:
        resolved = resolve_field_display(field_name, field_data)
        if resolved:
            ref = {
                "page_number": (field_data.get("source_reference") or {}).get("page_number"),
                "cell_ids": [field_data.get("cell_id")] if field_data.get("cell_id") else [],
                "cell_id": field_data.get("cell_id"),
                "bbox": field_data.get("bbox"),
            }
            return resolved, ref

    cell_id = (object_reference or {}).get("cell_id")
    cell = cell_by_id.get(cell_id) if cell_id else None
    if not cell:
        return None, None

    ref = {
        "page_number": cell.get("page_number"),
        "cell_ids": [cell_id],
        "cell_id": cell_id,
        "bbox": cell.get("bbox"),
    }
    return cell.get("text") or cell.get("normalized_value"), ref


def cell_matches_predicate(predicate: str, cell_text: str, *, resolved_value: str | None = None) -> bool:
    text = resolved_value or cell_text or ""
    if not text.strip():
        return False

    if predicate == "has_CAS":
        return bool(CAS_BRACKET_PATTERN.search(text) or CAS_TOKEN_PATTERN.search(text))

    if predicate in {"has_TWA", "has_STEL", "has_ceiling"}:
        return bool(EXPOSURE_PATTERN.search(text) or re.search(r"\b(ppm|mg/m³|mg/m3)\b", text, re.I))

    if predicate == "has_molecular_weight":
        return bool(re.search(r"[\d۰-۹]+(?:[./][\d۰-۹]+)?", text))

    if predicate == "has_health_effect":
        return bool(re.search(r"[\u0600-\u06FF]{4,}", text))

    if predicate == "has_symbols":
        return bool(re.search(r"\b(A[1-4]|DSEN|BEI|SKIN)\b", text, re.I))

    return True


def predicate_for_field(field_name: str) -> str | None:
    for predicate, mapped in PREDICATE_FIELD_MAP.items():
        if mapped == field_name:
            return predicate
    return None
