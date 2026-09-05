"""Deterministic slot extraction from Persian queries."""

from __future__ import annotations

import re
from typing import Any

from agents.routing.normalizer import strip_limit_type_prefix

CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
FORMULA_ID_PATTERN = re.compile(r"\b(formula_\d+_\d+)\b", re.IGNORECASE)
CONCENTRATION_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ppm|mg/m3|mg/m³|mg/m\^3|ppb|%)",
    re.IGNORECASE,
)
_ASSIGN = r"(?:=|:|برابر(?:\s*با)?)\s*"
AHW_PATTERN = re.compile(rf"ahw\s*_?\s*(\d+)\s*{_ASSIGN}(-?\d+(?:\.\d+)?)", re.IGNORECASE)
T_PATTERN = re.compile(rf"\bt\s*_?\s*(\d+)\s*{_ASSIGN}(-?\d+(?:\.\d+)?)", re.IGNORECASE)
CHEMICAL_LATIN = re.compile(r"\b([A-Za-z][a-zA-Z0-9\-()]+(?:\s+[a-zA-Z]+)?)\b")
NON_CHEMICAL_TOKENS = frozenset({"TWA", "STEL", "CAS", "MW", "BEI", "OEL", "CEILING", "C", "FOR"})
# Closed class: English document / measurement / grammar labels (not chemical names).
_LATIN_DOCUMENT_MEASUREMENT_LABELS = frozenset(
    {
        "number",
        "peak",
        "table",
        "page",
        "section",
        "chapter",
        "figure",
        "range",
        "value",
        "code",
        "type",
        "part",
        "unit",
        "limit",
        "level",
        "index",
        "factor",
        "ratio",
        "percent",
        "formula",
        "appendix",
        "note",
        "min",
        "max",
        "total",
        "mean",
        "sum",
        "from",
        "with",
        "and",
        "the",
    }
)

OEL_KEYWORDS = {
    "twa": "TWA",
    "stel": "STEL",
    "ceiling": "CEILING",
    "سقف": "CEILING",
}

# Session slots safe to inherit on genuine follow-ups — never chemical identity.
_INHERITABLE_SLOT_KEYS = frozenset({
    "oel_type",
    "formula_id",
    "variables",
    "concentration",
    "unit",
})


def _latin_token_segments(token: str) -> list[str]:
    return [part for part in re.split(r"[\s\-_()/]+", token) if part]


def is_non_chemical_latin_label(token: str) -> bool:
    """True for agency acronyms and document/measurement labels, not chemical names.

    Classes (not Gold-question words): all-caps ASCII acronyms of length >= 4,
    and a closed set of English document/measurement grammar labels. Short
    all-caps chemical abbreviations (e.g. EPN) remain eligible.
    """
    if not token:
        return True
    if token.upper() in NON_CHEMICAL_TOKENS:
        return True
    segments = _latin_token_segments(token) or [token]
    for seg in segments:
        if seg.upper() in NON_CHEMICAL_TOKENS:
            return True
        if seg.isascii() and seg.isalpha() and seg.isupper() and 4 <= len(seg) <= 8:
            return True
        if seg.lower() in _LATIN_DOCUMENT_MEASUREMENT_LABELS:
            return True
    return False


def _all_caps_acronym_in_query(query: str, token: str) -> bool:
    """True when this token appears in the query as a 4–8 letter ALL-CAPS acronym."""
    if not token.isascii() or not token.isalpha() or not (4 <= len(token) <= 8):
        return False
    for match in re.finditer(rf"\b{re.escape(token)}\b", query, re.IGNORECASE):
        if match.group(0).isupper():
            return True
    return False


def is_valid_cas(cas: str) -> bool:
    """Validate CAS Registry Number checksum (last digit = weighted sum mod 10)."""
    m = re.match(r"^(\d{2,7})-(\d{2})-(\d)$", cas.strip())
    if not m:
        return False
    digits = (m.group(1) + m.group(2))[::-1]
    total = sum(int(d) * (i + 1) for i, d in enumerate(digits))
    return total % 10 == int(m.group(3))


def extract_slots(query: str, inherited: dict[str, Any] | None = None) -> dict[str, Any]:
    inherited = inherited or {}
    slots: dict[str, Any] = {
        k: v for k, v in inherited.items() if k in _INHERITABLE_SLOT_KEYS
    }
    q = query

    m = CAS_PATTERN.search(q)
    if m:
        slots["cas"] = m.group(1)

    fm = FORMULA_ID_PATTERN.search(q)
    if fm:
        slots["formula_id"] = fm.group(1).lower()

    cm = CONCENTRATION_PATTERN.search(q)
    if cm:
        slots["concentration"] = float(cm.group(1))
        slots["unit"] = cm.group(2).replace("³", "3")

    if re.search(r"STEL\s*/\s*C|STEL-C|STEL_C", q, re.IGNORECASE):
        slots["oel_type"] = "STEL_C"
    else:
        for key, oel in OEL_KEYWORDS.items():
            if re.search(rf"\b{re.escape(key)}\b", q, re.IGNORECASE):
                slots["oel_type"] = oel
                break
    if re.search(r"\bMW\b|molecular\s*weight|وزن\s*مولکول|وزن\s*ملکول", q, re.IGNORECASE):
        slots["requested_field"] = "molecular_weight"
    elif re.search(r"symbols|نماد|notation", q, re.IGNORECASE):
        slots["requested_field"] = "symbols"

    # formula variable inputs
    ahw_inputs: dict[str, float] = {}
    for m in AHW_PATTERN.finditer(q):
        ahw_inputs[f"ahw_{m.group(1)}"] = float(m.group(2))
    t_inputs: dict[str, float] = {}
    for m in T_PATTERN.finditer(q):
        t_inputs[f"t_{m.group(1)}"] = float(m.group(2))
    if ahw_inputs or t_inputs:
        slots["variables"] = {**ahw_inputs, **t_inputs}

    if "chemical_name" not in slots:
        entity_query = strip_limit_type_prefix(q)
        chem = CHEMICAL_LATIN.search(entity_query)
        if chem:
            token = chem.group(1)
            if token.upper() not in NON_CHEMICAL_TOKENS and not is_non_chemical_latin_label(token):
                slots["chemical_name"] = token
        if "chemical_name" not in slots:
            lowered = entity_query.lower()
            for token in re.findall(r"\b[a-z][a-z0-9\-()]+\b", lowered):
                if (
                    token.upper() not in NON_CHEMICAL_TOKENS
                    and len(token) >= 4
                    and not is_non_chemical_latin_label(token)
                    and not _all_caps_acronym_in_query(entity_query, token)
                ):
                    slots["chemical_name"] = token
                    break

    # Persian common names
    persian_chems = {
        "بenzene": "Benzene",
        "بنزن": "Benzene",
        "تولوئن": "Toluene",
        "toluene": "Toluene",
        "استایرن": "Styrene",
    }
    for fa, en in persian_chems.items():
        if fa.lower() in q.lower():
            slots["chemical_name"] = en

    return slots
