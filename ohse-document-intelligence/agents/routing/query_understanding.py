"""Query understanding — normalization, slots, chemical resolution, query type."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents.routing.chemical_resolver import ChemicalResolver, ChemicalResolution
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots
from sqlalchemy.orm import Session

# Abbreviation / alias normalization (applied after base normalizer)
ABBREVIATION_MAP = {
    r"\bمیانگین\s*وزنی\b": "TWA",
    r"\bکوتاه\s*مدت\b": "STEL",
    r"\bحد\s*سقف\b": "Ceiling",
    r"\bحد\s*سقفی\b": "Ceiling",
    r"\bمواجهه\s*مجاز\b": "OEL",
    r"\bBEI\b": "BEI",
    r"\bA\s*\(\s*8\s*\)": "A(8)",
}

OEL_ALIASES = {
    "twa": "TWA", "stel": "STEL", "ceiling": "CEILING", "c": "CEILING",
    "سقف": "CEILING", "میانگین": "TWA",
}


class QueryType(str, Enum):
    NUMERIC_LOOKUP = "numeric_lookup"
    DEFINITION = "definition"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    FORMULA_CALC = "formula_calc"
    FOLLOW_UP = "follow_up"
    AMBIGUOUS = "ambiguous"
    GENERAL = "general"


@dataclass
class QueryUnderstanding:
    raw_query: str
    normalized_query: str
    query_type: QueryType
    slots: dict[str, Any] = field(default_factory=dict)
    chemical: ChemicalResolution | None = None
    requires_clarification: bool = False
    clarification_reason: str | None = None
    trace: list[str] = field(default_factory=list)


def enhance_normalization(query: str) -> str:
    q = normalize_persian_query(query)
    for pattern, repl in ABBREVIATION_MAP.items():
        q = re.sub(pattern, repl, q, flags=re.IGNORECASE)
    return q.strip()


def classify_query_type(query: str, slots: dict[str, Any]) -> QueryType:
    q = query.lower()
    if re.search(r"وزن\s*مولکول|molecular\s*weight|\bMW\b", q, re.I):
        return QueryType.NUMERIC_LOOKUP
    if slots.get("variables") or re.search(r"ahw\d|محاسبه|حساب\s*کن|compute", q, re.I):
        return QueryType.FORMULA_CALC
    if re.search(r"(یعنی|تعریف|چیست|چیه|معنا)", q) and not re.search(r"\d+\s*ppm", q):
        return QueryType.DEFINITION
    if re.search(r"(توضیح|چرا|چگونه|تفاوت)", q):
        return QueryType.EXPLANATION
    if re.search(r"(بیشتر|مقایسه|نسبت|ppm|مواجهه\s*من)", q, re.I):
        return QueryType.COMPARISON
    if re.search(r"(حد|TWA|STEL|CAS|MW|چقدر|چنده)", q, re.I) and (slots.get("chemical_name") or slots.get("cas")):
        return QueryType.NUMERIC_LOOKUP
    if len(q) < 35 and re.search(r"(نداره|STEL|TWA|CAS|؟)", q):
        return QueryType.FOLLOW_UP
    if not slots.get("chemical_name") and re.search(r"(حد|چقدر|چنده)", q) and len(q) < 30:
        return QueryType.AMBIGUOUS
    return QueryType.GENERAL


def understand_query(
    session: Session | None,
    query: str,
    *,
    inherited_slots: dict[str, Any] | None = None,
    raw_query: str | None = None,
) -> QueryUnderstanding:
    inherited_slots = inherited_slots or {}
    raw_query = raw_query or query
    normalized = enhance_normalization(query)
    slots = extract_slots(normalized, inherited_slots)
    trace = ["normalized"]

    chemical: ChemicalResolution | None = None
    if session is not None:
        resolver = ChemicalResolver(session)
        chemical = resolver.resolve(
            normalized,
            inherited=inherited_slots,
            raw_query=raw_query,
        )
        trace.append(f"chemical:{chemical.method}")
        if chemical.ambiguous:
            slots.pop("chemical_name", None)
            slots.pop("cas", None)
            slots.pop("chemical_id", None)
            slots["ambiguous_chemical"] = True
            return QueryUnderstanding(
                raw_query=raw_query,
                normalized_query=normalized,
                query_type=QueryType.AMBIGUOUS,
                slots=slots,
                chemical=chemical,
                requires_clarification=True,
                clarification_reason="ambiguous_chemical",
                trace=trace,
            )
        if chemical.canonical_name and chemical.confidence >= 0.5:
            slots.update(chemical.to_slot_dict())

    qtype = classify_query_type(normalized, slots)
    trace.append(f"type:{qtype.value}")

    requires_clarify = False
    reason = None
    if qtype == QueryType.AMBIGUOUS and not slots.get("chemical_name"):
        requires_clarify = True
        reason = "missing_chemical"

    return QueryUnderstanding(
        raw_query=raw_query,
        normalized_query=normalized,
        query_type=qtype,
        slots=slots,
        chemical=chemical,
        requires_clarification=requires_clarify,
        clarification_reason=reason,
        trace=trace,
    )
