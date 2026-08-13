"""PDF search query variants for geometry promotion."""

from __future__ import annotations

import re

from ingestion.pdf_geometry import normalize_match_text

LIMIT_UNIT_SPACING_RE = re.compile(
    r"^([\d۰-۹٠-٩./\s-]+)\s*(ppm|ppb|mg/m3|mg/m³|f/ml|mg/m\^?\{?3\}?)\s*$",
    re.IGNORECASE,
)


def compact_match_text(value: str) -> str:
    return normalize_match_text(value).replace(" ", "")


def search_query_variants(text: str) -> list[str]:
    normalized = normalize_match_text(text)
    compact = compact_match_text(text)
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        variants.append(value)

    add(text)
    add(normalized)
    add(compact)

    match = LIMIT_UNIT_SPACING_RE.match(normalized.replace(" ", ""))
    if match:
        add(f"{match.group(1).strip()} {match.group(2).lower()}")
    elif "ppm" in normalized and " " not in normalized:
        add(re.sub(r"(?<=\d)(?=ppm)", " ", normalized, flags=re.IGNORECASE))

    return variants
