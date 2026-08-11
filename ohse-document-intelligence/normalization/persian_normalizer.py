"""Persian/Arabic text normalization — always preserve original."""

from __future__ import annotations

import re
from dataclasses import dataclass

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
LATIN_DIGITS = "0123456789"

CHAR_MAP = {
    "ي": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
    "‌": " ",  # ZWNJ to space for search; original preserved separately
}

PERSIAN_TO_LATIN_DIGITS = str.maketrans(
    {p: l for p, l in zip(PERSIAN_DIGITS + ARABIC_DIGITS, LATIN_DIGITS * 2)}
)


@dataclass(frozen=True)
class NormalizedText:
    original: str
    normalized: str


def normalize_persian_text(value: str) -> NormalizedText:
    if not value:
        return NormalizedText(original=value, normalized=value)

    normalized = value.strip()
    for src, dst in CHAR_MAP.items():
        normalized = normalized.replace(src, dst)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.translate(PERSIAN_TO_LATIN_DIGITS)
    return NormalizedText(original=value, normalized=normalized)


def contains_persian(value: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", value))
