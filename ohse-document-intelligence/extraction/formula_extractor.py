"""Formula extraction from table and text content.

Legacy regex helpers — prefer extraction.formula_candidate_detector for new pipeline work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from extraction.formula_candidate_detector import detect_formula_candidates

FORMULA_REGEX = re.compile(
    r"(?P<name>[A-Za-z_\w]+\([^)]+\)|[A-Za-z_\w]+)\s*=\s*(?P<expr>[^;\n]+)",
)


@dataclass(frozen=True)
class ExtractedFormula:
    formula_name: str | None
    original_expression: str
    normalized_expression: str
    variables: dict[str, str]
    page_number: int


def _normalize_expression(expression: str) -> str:
    expr = expression.strip()
    expr = expr.replace("sqrt", "sqrt").replace("√", "sqrt")
    expr = re.sub(r"\s+", "", expr)
    return expr


def extract_formula_candidates(text: str, page_number: int) -> list[dict]:
    """Return formula candidates only — not validated gold formulas."""
    return [
        candidate.to_dict()
        for candidate in detect_formula_candidates(page_number, text, paragraphs=[])
    ]


def extract_formulas(text: str, page_number: int) -> list[ExtractedFormula]:
    """Deprecated: regex-based extraction retained for process_document.py compatibility."""
    formulas: list[ExtractedFormula] = []
    for match in FORMULA_REGEX.finditer(text):
        name = match.group("name").strip()
        original = match.group(0).strip()
        expr = match.group("expr").strip()
        variables: dict[str, str] = {}
        for var in re.findall(r"\b[A-Za-z]+\b", expr):
            if var.lower() not in {"sqrt", "log", "exp", "sin", "cos"}:
                variables.setdefault(var, "unknown")
        formulas.append(
            ExtractedFormula(
                formula_name=name,
                original_expression=original,
                normalized_expression=_normalize_expression(expr),
                variables=variables,
                page_number=page_number,
            )
        )
    return formulas
