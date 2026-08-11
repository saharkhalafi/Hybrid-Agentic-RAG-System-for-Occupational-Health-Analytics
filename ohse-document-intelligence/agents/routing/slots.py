"""Deterministic slot extraction from Persian queries."""

from __future__ import annotations

import re
from typing import Any

CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
FORMULA_ID_PATTERN = re.compile(r"\b(formula_\d+_\d+)\b", re.IGNORECASE)
CONCENTRATION_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ppm|mg/m3|mg/m³|mg/m\^3|ppb|%)",
    re.IGNORECASE,
)
_ASSIGN = r"(?:=|:|برابر(?:\s*با)?)\s*"
AHW_PATTERN = re.compile(rf"ahw\s*_?\s*(\d+)\s*{_ASSIGN}(-?\d+(?:\.\d+)?)", re.IGNORECASE)
T_PATTERN = re.compile(rf"\bt\s*_?\s*(\d+)\s*{_ASSIGN}(-?\d+(?:\.\d+)?)", re.IGNORECASE)
CHEMICAL_LATIN = re.compile(r"\b([A-Z][a-zA-Z0-9\-()]+(?:\s+[a-zA-Z]+)?)\b")

OEL_KEYWORDS = {
    "twa": "TWA",
    "stel": "STEL",
    "ceiling": "CEILING",
    "سقف": "CEILING",
}


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
    slots: dict[str, Any] = dict(inherited)
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

    for key, oel in OEL_KEYWORDS.items():
        if re.search(rf"\b{re.escape(key)}\b", q, re.IGNORECASE):
            slots["oel_type"] = oel
            break

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
        chem = CHEMICAL_LATIN.search(q)
        if chem:
            token = chem.group(1)
            if token.upper() not in {"TWA", "STEL", "CAS", "MW", "BEI", "OEL", "CEILING"}:
                slots["chemical_name"] = token

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
