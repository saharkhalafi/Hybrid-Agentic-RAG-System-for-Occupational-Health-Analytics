"""Page triage — classify pages before expensive OCR processing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from database.models import PageType
from ingestion.pdf_loader import PageContent

DOMAIN_KEYWORD_PATTERN = re.compile(
    r"\b(CAS|TWA|STEL|OEL|VDV|LAeq|dBA|BEI|ppm|mg/m3|mg/m³)\b",
    re.IGNORECASE,
)
EQUATION_PATTERN = re.compile(
    r"(?:[A-Za-z_\)\]]+\s*=\s*[^=\n]{3,})|(?:\bsqrt\s*\()|(?:\([^)]+\)\s*/\s*[^=\n]+)|(?:\b\d+\s*/\s*\d+\b)",
    re.IGNORECASE,
)
MATH_OPERATOR_PATTERN = re.compile(r"(?<![A-Za-z])(?:\^|\+|\-|\*|/)(?![A-Za-z])|√")
VARIABLE_UNIT_PATTERN = re.compile(
    r"\b[A-Za-z]\s*=\s*[\d.]+\s*(?:m/s|mg/m3|mg/m³|ppm|dBA|hours?|hr)\b",
    re.IGNORECASE,
)
PERSIAN_PATTERN = re.compile(r"[\u0600-\u06FF]")


@dataclass(frozen=True)
class PageTriageResult:
    page_number: int
    page_type: PageType
    has_digital_text: bool
    table_score: float
    formula_score: float
    text_density: float
    persian_ratio: float
    confidence: float
    has_domain_keywords: bool
    has_mathematical_formula: bool
    metadata: dict


def _score_domain_keywords(text: str) -> float:
    matches = DOMAIN_KEYWORD_PATTERN.findall(text)
    return min(len(matches) / 5.0, 1.0)


def _score_mathematical_formulas(text: str) -> float:
    score = 0.0
    if EQUATION_PATTERN.search(text):
        score += 0.5
    if MATH_OPERATOR_PATTERN.search(text):
        score += 0.2
    if VARIABLE_UNIT_PATTERN.search(text):
        score += 0.3
    return min(score, 1.0)


def classify_page(page: PageContent) -> PageTriageResult:
    text = page.text or ""
    char_count = max(len(text), 1)
    persian_chars = len(PERSIAN_PATTERN.findall(text))
    persian_ratio = persian_chars / char_count
    domain_score = _score_domain_keywords(text)
    formula_score = _score_mathematical_formulas(text)
    text_density = min(char_count / 2000.0, 1.0)
    has_domain_keywords = domain_score >= 0.3
    has_mathematical_formula = formula_score >= 0.5

    if not page.has_digital_text:
        page_type = PageType.SCANNED
        if domain_score >= 0.4:
            page_type = PageType.TABLE_HEAVY
        confidence = 0.75
    elif domain_score >= 0.6 and not has_mathematical_formula:
        page_type = PageType.TABLE_HEAVY
        confidence = 0.85
    elif has_mathematical_formula:
        page_type = PageType.FORMULA_HEAVY
        confidence = 0.8
    elif text_density >= 0.5 and domain_score < 0.3:
        page_type = PageType.TEXT_HEAVY
        confidence = 0.8
    elif domain_score >= 0.3 and has_mathematical_formula:
        page_type = PageType.MIXED
        confidence = 0.7
    elif page.has_digital_text:
        page_type = PageType.DIGITAL_TEXT
        confidence = 0.85
    else:
        page_type = PageType.UNKNOWN
        confidence = 0.5

    return PageTriageResult(
        page_number=page.page_number,
        page_type=page_type,
        has_digital_text=page.has_digital_text,
        table_score=domain_score,
        formula_score=formula_score,
        text_density=text_density,
        persian_ratio=persian_ratio,
        confidence=confidence,
        has_domain_keywords=has_domain_keywords,
        has_mathematical_formula=has_mathematical_formula,
        metadata={
            "width": page.width,
            "height": page.height,
            "char_count": char_count,
        },
    )
