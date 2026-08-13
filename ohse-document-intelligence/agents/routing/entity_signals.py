"""Detect whether a query explicitly names a chemical entity (Persian, Latin, or CAS)."""

from __future__ import annotations

import re

CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
LATIN_CHEMICAL = re.compile(r"\b([A-Z][a-zA-Z0-9\-()]+(?:\s+[a-zA-Z]+)?)\b")
PERSIAN_TOKEN = re.compile(r"[\u0600-\u06FF]{3,}")

# Common HSE query words — not chemical names on their own.
_PERSIAN_STOPWORDS = frozenset({
    "برای", "چقدر", "چقدره", "چنده", "چند", "حد", "مجاز", "مواجهه", "مقدار", "چیست", "چیه",
    "یعنی", "تعریف", "توضیح", "منبع", "صفحه", "جدول", "میانگین", "وزنی",
    "کوتاه", "مدت", "سقف", "حدود", "شغلی", "ppm", "است", "دارد", "داره",
    "نداره", "بیشتر", "کمتر", "مقایسه", "فرمول", "محاسبه", "وزن", "مولکول",
    "ملکول", "ملکولی", "نام", "علمی", "ماده", "شیمیایی",
    "میشه", "می‌شه", "چی",
})

_LIMIT_TYPE_TOKENS = frozenset({
    "twa", "twe", "tve", "stel", "ceiling", "oel", "cas", "mw", "bei", "c",
})

_MALFORMED_CAS = re.compile(r"\b[a-zA-Z0-9]{2,}(?:-[a-zA-Z0-9]{2,}){2,}\b")

_NON_CHEMICAL_LATIN = frozenset({
    "TWA", "STEL", "CAS", "MW", "BEI", "OEL", "CEILING", "PPM", "PPB",
})


def query_has_entity_attempt(query: str) -> bool:
    """True when the user appears to name a chemical, even if resolution may fail."""
    if query_has_explicit_entity_signal(query):
        return True
    return _MALFORMED_CAS.search(query or "") is not None


def extract_persian_entity_tokens(query: str) -> list[str]:
    tokens = PERSIAN_TOKEN.findall(query or "")
    cleaned: list[str] = []
    for t in tokens:
        t = t.rstrip("?؟").strip()
        if t and t not in _PERSIAN_STOPWORDS and t.lower() not in _LIMIT_TYPE_TOKENS:
            cleaned.append(t)
    return cleaned


def query_has_explicit_entity_signal(query: str) -> bool:
    """True when the query itself names a chemical (not merely a limit-type follow-up)."""
    q = (query or "").strip()
    if not q:
        return False
    if CAS_PATTERN.search(q):
        return True
    for m in LATIN_CHEMICAL.finditer(q):
        token = m.group(1)
        if token.upper() not in _NON_CHEMICAL_LATIN:
            return True
    persian = extract_persian_entity_tokens(q)
    if not persian:
        return False
    # Multi-word Persian phrases or a single substantive token (>=4 chars) name a chemical.
    if len(persian) >= 2:
        return True
    return len(persian[0]) >= 4
