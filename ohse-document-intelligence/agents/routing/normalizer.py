"""Persian query normalization."""

from __future__ import annotations

import re

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Common OCR / typing variants in Persian OHSE queries (not query-specific hardcoding).
_QUERY_TYPO_REPLACEMENTS = (
    (r"ملکول", "مولکول"),
    (r"ملکولی", "مولکولی"),
    (r"موکول", "مولکول"),
    (r"موکولی", "مولکولی"),
)


def normalize_persian_query(query: str) -> str:
    q = (query or "").strip()
    q = q.translate(FA_DIGITS).translate(AR_DIGITS)
    q = re.sub(r"\s+", " ", q)
    q = q.replace("؟", "?").replace("‌", " ")
    for pattern, repl in _QUERY_TYPO_REPLACEMENTS:
        q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
    return q.strip()
