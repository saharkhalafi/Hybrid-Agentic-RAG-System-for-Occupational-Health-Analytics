"""Gate table detection and trigger recovery when Document AI misses chemical OEL tables."""

from __future__ import annotations

import re
from typing import Any

CHEMICAL_OEL_PERSIAN_SIGNATURES = (
    "نام علمی ماده شیمیایی",
    "وزن مولکولی",
    "حد مجاز مواجهه",
    "نمادها",
    "مبنای تعیین حد",
    "ردیف",
)

CHEMICAL_OEL_ENGLISH_SIGNATURES = (
    "CAS",
    "TWA",
    "STEL",
    "Ceiling",
    "Molecular Weight",
    "TLV",
    "OEL",
)

CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")


def has_chemical_oel_signatures(page_text: str) -> bool:
    text = page_text or ""
    persian_hits = sum(1 for sig in CHEMICAL_OEL_PERSIAN_SIGNATURES if sig in text)
    english_hits = sum(
        1 for sig in CHEMICAL_OEL_ENGLISH_SIGNATURES if re.search(rf"\b{re.escape(sig)}\b", text, re.I)
    )
    has_cas = bool(CAS_PATTERN.search(text))
    return persian_hits >= 2 and english_hits >= 2 and has_cas


def evaluate_table_detection(
    *,
    document_type: str,
    page_text: str,
    document_ai_tables: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return table detection status and whether PyMuPDF recovery should run."""
    if document_ai_tables:
        return {
            "table_detection_status": "detected",
            "needs_recovery": False,
            "recovery_method": None,
        }

    if document_type == "chemical_oel_table" and has_chemical_oel_signatures(page_text):
        return {
            "table_detection_status": "missed_by_document_ai",
            "needs_recovery": True,
            "recovery_method": "pymupdf_geometry",
        }

    if document_type == "chemical_oel_table":
        return {
            "table_detection_status": "missed_by_document_ai",
            "needs_recovery": True,
            "recovery_method": "pymupdf_geometry",
        }

    return {
        "table_detection_status": "not_applicable",
        "needs_recovery": False,
        "recovery_method": None,
    }
