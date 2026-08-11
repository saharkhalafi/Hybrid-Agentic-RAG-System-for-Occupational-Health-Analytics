"""Deterministic page metadata detection — no LLM."""

from __future__ import annotations

import re
from typing import Any

from database.models import TableType
from goldset_generator.table_detection_gate import has_chemical_oel_signatures

DEFINITION_KEYWORDS = re.compile(
    r"\b(تعریف|definition|اصطلاح|glossary|واژه)\b", re.IGNORECASE
)
REGULATION_KEYWORDS = re.compile(
    r"\b(مقررات|آیین|بند|ماده|regulation|standard|ISO|ACGIH|OSHA)\b", re.IGNORECASE
)
INTRO_KEYWORDS = re.compile(
    r"\b(مقدمه|introduction|پیشگفتار|فهرست|contents)\b", re.IGNORECASE
)
FORMULA_PATTERN = re.compile(
    r"(=|\\sqrt|√|∑|Σ|\^|\{|\}|\\cdot|/|\ba_w\b|T\s*=|\bVDV\b)",
    re.IGNORECASE,
)
CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")


def detect_document_type(page_text: str, tables: list[dict[str, Any]]) -> str:
    text = page_text or ""
    table_types = {table.get("table_type") for table in tables}

    if TableType.CHEMICAL_OEL.value in table_types or (
        CAS_PATTERN.search(text) and re.search(r"\b(TWA|STEL|OEL)\b", text, re.IGNORECASE)
    ):
        return "chemical_oel_table"
    if TableType.VIBRATION.value in table_types:
        return "vibration"
    if TableType.NOISE.value in table_types:
        return "noise"
    if TableType.BIOLOGICAL_MONITORING.value in table_types:
        return "biological_monitoring"
    if DEFINITION_KEYWORDS.search(text):
        return "definition"
    if REGULATION_KEYWORDS.search(text):
        return "regulation"
    if INTRO_KEYWORDS.search(text):
        return "introduction"
    if tables:
        return "unknown"
    return "unknown"


def contains_formula(page_text: str) -> bool:
    text = page_text or ""
    if not FORMULA_PATTERN.search(text):
        return False
    if has_chemical_oel_signatures(text):
        return bool(re.search(r"(=|\\sqrt|√|∑|Σ|\ba_w\b|T\s*=|\bVDV\b)", text, re.IGNORECASE))
    return True


def generate_page_metadata(
    page_number: int,
    page_text: str,
    tables: list[dict[str, Any]],
    *,
    pdf_page_number: int | None = None,
    printed_page_number: int | None = None,
    table_detection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pdf_page = pdf_page_number if pdf_page_number is not None else page_number
    detection = table_detection or {}
    recovery_used = detection.get("table_detection_status") == "recovered"
    contains_table = bool(tables) or recovery_used

    metadata = {
        "page_number": pdf_page,
        "pdf_page_number": pdf_page,
        "printed_page_number": printed_page_number,
        "document_type": detect_document_type(page_text, tables),
        "contains_table": contains_table,
        "contains_formula": contains_formula(page_text),
        "contains_definition": bool(DEFINITION_KEYWORDS.search(page_text or "")),
        "contains_regulation": bool(REGULATION_KEYWORDS.search(page_text or "")),
        "table_count": len(tables),
        "detection_method": "deterministic_rules",
    }
    if detection:
        metadata["table_detection_status"] = detection.get("table_detection_status")
        metadata["table_recovery_used"] = recovery_used
        if detection.get("recovery_method"):
            metadata["recovery_method"] = detection["recovery_method"]
    return metadata
