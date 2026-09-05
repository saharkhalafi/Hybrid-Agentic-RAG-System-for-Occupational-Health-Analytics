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
OEL_LIMIT_UNIT_PATTERN = re.compile(r"\bppm\b|mg/m", re.IGNORECASE)


def has_chemical_oel_signatures(page_text: str) -> bool:
    text = page_text or ""
    persian_hits = sum(1 for sig in CHEMICAL_OEL_PERSIAN_SIGNATURES if sig in text)
    english_hits = sum(
        1 for sig in CHEMICAL_OEL_ENGLISH_SIGNATURES if re.search(rf"\b{re.escape(sig)}\b", text, re.I)
    )
    has_cas = bool(CAS_PATTERN.search(text))
    return persian_hits >= 2 and english_hits >= 2 and has_cas


OEL_CONTINUATION_PAGE_START = 46
OEL_CONTINUATION_PAGE_END = 161


def looks_like_oel_table_page(page_text: str, page_number: int | None = None) -> bool:
    """Headered OEL pages anywhere; continuation body pages only in the HSE6 OEL band."""
    if has_chemical_oel_signatures(page_text):
        return True
    text = page_text or ""
    headered = (
        "نام علمی ماده شیمیایی" in text
        and re.search(r"\bTWA\b", text, re.I) is not None
        and re.search(r"\bSTEL\b", text, re.I) is not None
    )
    if headered:
        return True
    if page_number is not None and not (
        OEL_CONTINUATION_PAGE_START <= page_number <= OEL_CONTINUATION_PAGE_END
    ):
        return False
    if not CAS_PATTERN.search(text):
        return False
    return bool(OEL_LIMIT_UNIT_PATTERN.search(text))


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

    if document_type == "chemical_oel_table" and looks_like_oel_table_page(page_text):
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
