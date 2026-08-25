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

# Common limit-type typos / abbreviations in Persian HSE queries.
_LIMIT_TYPE_TYPO_REPLACEMENTS = (
    (r"\btwe\b", "TWA"),
    (r"\btve\b", "TWA"),
    (r"\btwa\b", "TWA"),
    (r"\bstel\b", "STEL"),
    (r"\bceiling\b", "Ceiling"),
)

_LIMIT_TYPE_LEADING_RE = re.compile(
    r"^(?:TWA|STEL|CEILING|C)\s+(?:for\s+)?(.+)$",
    re.IGNORECASE,
)


def strip_limit_type_prefix(query: str) -> str:
    """Remove leading limit-type tokens so entity phrases can be resolved (e.g. 'TWA benzene')."""
    q = (query or "").strip()
    match = _LIMIT_TYPE_LEADING_RE.match(q)
    if match:
        return match.group(1).strip()
    return q


def normalize_persian_query(query: str) -> str:
    q = (query or "").strip()
    q = q.translate(FA_DIGITS).translate(AR_DIGITS)
    q = re.sub(r"\s+", " ", q)
    q = q.replace("؟", "?").replace("‌", " ")
    for pattern, repl in _QUERY_TYPO_REPLACEMENTS:
        q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
    for pattern, repl in _LIMIT_TYPE_TYPO_REPLACEMENTS:
        q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
    return q.strip()
