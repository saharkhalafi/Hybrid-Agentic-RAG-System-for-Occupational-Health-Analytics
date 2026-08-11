"""Exposure limit validation rules."""

from __future__ import annotations

import re

EXPOSURE_PATTERN = re.compile(r"[\d۰-۹]+(?:[./][\d۰-۹]+)?\s*(ppm|mg/m³|mg/m3|f/ml)", re.I)


def validate_exposure(value: str | None, unit: str | None = None) -> list[str]:
    if not value:
        return []
    combined = f"{value} {unit or ''}".strip()
    if not EXPOSURE_PATTERN.search(combined) and not re.search(r"[\d۰-۹]", str(value)):
        return [f"exposure value not numeric: {value!r}"]
    return []
