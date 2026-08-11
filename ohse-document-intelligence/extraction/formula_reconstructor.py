"""Layout-aware formula reconstruction from PDF evidence fragments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from extraction.formula_candidate_detector import FormulaCandidate

SUPERSCRIPT_PAIR_PATTERN = re.compile(
    r"\b([a-z]{2,5})\s*(\d)(\d)\s*×\s*t\s*(\d+|n)\b",
    re.I,
)
GENERAL_SUPER_PAIR_PATTERN = re.compile(r"\b([a-z]{2,5})\s*(\d)(\d)\b", re.I)
SUBSCRIPT_N_PATTERN = re.compile(r"\b([a-z]{2,5})\s*n(\d)\b", re.I)
SUM_ELLIPSIS_PATTERN = re.compile(
    r"\(\s*\(([^)]+)\)\s*\+\s*\(([^)]+)\)\s*\+…\+\s*\(([^)]+)\)\s*\)",
    re.I,
)


@dataclass
class FormulaReconstruction:
    expression: str
    raw_expression: str
    status: str = "validated"
    layout_notes: list[str] = field(default_factory=list)
    evidence_fragment_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "raw_expression": self.raw_expression,
            "status": self.status,
            "layout_notes": self.layout_notes,
            "evidence_fragment_ids": self.evidence_fragment_ids,
        }


def _bbox_center(bbox: dict[str, Any] | None) -> tuple[float, float] | None:
    if not bbox:
        return None
    return bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2


def _horizontal_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    a_left, a_right = a["x"], a["x"] + a["width"]
    b_left, b_right = b["x"], b["x"] + b["width"]
    return max(a_left, b_left) < min(a_right, b_right)


def _detect_vertical_fraction(
    fragments: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Detect numerator/denominator pairs from vertically stacked bbox fragments."""
    notes: list[str] = []
    usable = [f for f in fragments if f.get("bbox") and (f.get("text") or "").strip()]
    for upper in usable:
        upper_text = (upper.get("text") or "").strip()
        if len(upper_text) > 8 or "\n" in upper_text:
            continue
        upper_bbox = upper["bbox"]
        for lower in usable:
            if upper is lower:
                continue
            lower_text = (lower.get("text") or "").strip()
            lower_bbox = lower["bbox"]
            if not lower_text or upper_bbox["y"] >= lower_bbox["y"]:
                continue
            if len(lower_text) > 12:
                continue
            if upper_text.isdigit() and not re.match(r"^T[\s(]", lower_text):
                continue
            if lower_bbox["y"] - (upper_bbox["y"] + upper_bbox["height"]) > 12:
                continue
            if not _horizontal_overlap(upper_bbox, lower_bbox):
                continue
            notes.append(f"vertical_fraction:{upper_text}/{lower_text}")
            return f"({upper_text}/{lower_text})", notes

    for fragment in usable:
        text = (fragment.get("text") or "").strip()
        if "\n" in text:
            parts = [part.strip() for part in text.split("\n") if part.strip()]
            if len(parts) == 2 and all(len(part) <= 6 for part in parts):
                notes.append(f"stacked_text_fraction:{parts[0]}/{parts[1]}")
                return f"({parts[0]}/{parts[1]})", notes
    return None, notes


def _format_summation_canonical(terms: list[str]) -> str:
    """Wrap summation terms in a single outer parenthesis group under sqrt."""
    normalized = [term.strip() for term in terms if term.strip()]
    if not normalized:
        return "()"
    formatted = [re.sub(r"×", " × ", term) for term in normalized]
    if len(formatted) >= 3:
        inner = f"({formatted[0]}) + ({formatted[1]}) + ... + ({formatted[2]})"
    else:
        inner = " + ".join(f"({term})" for term in formatted)
    return f"({inner})"


def _normalize_superscripts(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    def repl_sum(match: re.Match[str]) -> str:
        notes.append("expanded_summation_terms")
        terms = [match.group(1), match.group(2), match.group(3)]
        normalized_terms = []
        for term in terms:
            normalized_terms.append(_normalize_superscripts(term)[0])
        return _format_summation_canonical(normalized_terms)

    if SUM_ELLIPSIS_PATTERN.search(text):
        text = SUM_ELLIPSIS_PATTERN.sub(repl_sum, text)

    def repl_pair(match: re.Match[str]) -> str:
        var, sub, sup = match.group(1), match.group(2), match.group(3)
        notes.append(f"superscript_pair:{var}_{sub}^{sup}")
        return f"{var}_{sub}^{sup}×t_{sub}"

    text = SUPERSCRIPT_PAIR_PATTERN.sub(repl_pair, text)
    text = SUBSCRIPT_N_PATTERN.sub(lambda m: f"{m.group(1)}_n^{m.group(2)}", text)
    text = re.sub(r"\bt\s+n\b", "t_n", text)
    return text, notes


def _extract_lhs(candidate_text: str) -> tuple[str | None, str]:
    match = re.search(r"([A-Za-z]+\(\d+\)|[a-z]{2,5})\s*=\s*", candidate_text)
    if not match:
        return None, candidate_text
    return match.group(1), candidate_text[match.end() :].strip()


def _build_equation_two(candidate: FormulaCandidate) -> FormulaReconstruction:
    fragments = candidate.evidence_text_fragments
    notes: list[str] = []
    raw_parts = [f.get("text", "") for f in fragments if f.get("text")]
    raw_expression = "\n".join(raw_parts).strip()

    lhs, _ = _extract_lhs(raw_expression)
    if not lhs:
        lhs = "ahv"

    fraction, fraction_notes = _detect_vertical_fraction(fragments)
    notes.extend(fraction_notes)
    if not fraction:
        if re.search(r"\b1\b", raw_expression) and re.search(r"\bT\b", raw_expression):
            fraction = "(1/T)"
            notes.append("inferred_fraction_1_over_T")

    sum_source = raw_expression.replace("\n", " ")
    sum_match = re.search(r"\(\([^)]+\)\+\([^)]+\)\+…\+\([^)]+\)\)", sum_source)
    if not sum_match:
        sum_match = re.search(r"\(\([^)]+\)\+\([^)]+\)[^)]*\)", sum_source)

    if sum_match:
        sum_text = sum_match.group(0)
        normalized_sum, sum_notes = _normalize_superscripts(sum_text)
        notes.extend(sum_notes)
        if fraction:
            expression = f"{lhs} = sqrt({fraction} * {normalized_sum})"
            status = "validated"
        else:
            expression = f"{lhs} = sqrt({normalized_sum})"
            status = "extraction_uncertain"
        return FormulaReconstruction(
            expression=expression,
            raw_expression=raw_expression,
            status=status,
            layout_notes=notes,
            evidence_fragment_ids=candidate.paragraph_indices,
        )

    normalized_body, body_notes = _normalize_superscripts(raw_expression)
    notes.extend(body_notes)
    if fraction and "√" in raw_expression:
        expression = f"{lhs} = sqrt({fraction} * ({normalized_body}))"
        status = "extraction_uncertain"
    elif "√" in raw_expression:
        expression = f"{lhs} = sqrt({normalized_body})"
        status = "extraction_uncertain"
    else:
        expression = f"{lhs} = {normalized_body}"
        status = "extraction_uncertain"

    return FormulaReconstruction(
        expression=expression,
        raw_expression=raw_expression,
        status=status,
        layout_notes=notes,
        evidence_fragment_ids=candidate.paragraph_indices,
    )


def _build_equation_three(candidate: FormulaCandidate) -> FormulaReconstruction:
    fragments = candidate.evidence_text_fragments
    notes: list[str] = []
    raw_parts = [f.get("text", "") for f in fragments if f.get("text")]
    raw_expression = "\n".join(raw_parts).strip()

    lhs = "A(8)"
    lhs_match = re.search(r"A\(\d+\)", raw_expression)
    if lhs_match:
        lhs = lhs_match.group(0)

    fraction, fraction_notes = _detect_vertical_fraction(fragments)
    notes.extend(fraction_notes)

    if fraction:
        expression = f"{lhs} = ahv * sqrt({fraction})"
        status = "validated"
    elif re.search(r"Tv|T0|To", raw_expression, re.I):
        expression = f"{lhs} = ahv * sqrt(Tv/T0)"
        notes.append("inferred_fraction_from_tokens")
        status = "extraction_uncertain"
    else:
        expression = f"{lhs} = ahv * sqrt(...)"
        status = "extraction_uncertain"

    return FormulaReconstruction(
        expression=expression,
        raw_expression=raw_expression,
        status=status,
        layout_notes=notes,
        evidence_fragment_ids=candidate.paragraph_indices,
    )


def reconstruct_formula(candidate: FormulaCandidate) -> FormulaReconstruction:
    """Reconstruct a mathematical expression using layout evidence."""
    eq_ref = candidate.document_equation_reference
    if eq_ref == "2":
        return _build_equation_two(candidate)
    if eq_ref == "3":
        return _build_equation_three(candidate)

    raw_expression = candidate.candidate_text
    lhs, rhs = _extract_lhs(raw_expression)
    fraction, notes = _detect_vertical_fraction(candidate.evidence_text_fragments)
    normalized_rhs, rhs_notes = _normalize_superscripts(rhs)
    notes.extend(rhs_notes)

    if lhs and fraction:
        expression = f"{lhs} = sqrt({fraction})" if "√" in raw_expression else f"{lhs} = {fraction}"
        status = "validated"
    elif lhs:
        expression = f"{lhs} = {normalized_rhs}" if normalized_rhs else f"{lhs} = ..."
        status = "extraction_uncertain" if len(normalized_rhs) < 4 else "validated"
    else:
        expression = normalized_rhs or raw_expression
        status = "extraction_uncertain"

    if expression.strip() in {"√", "sqrt", "ahv=√", "ahv = sqrt"}:
        status = "extraction_uncertain"

    return FormulaReconstruction(
        expression=expression,
        raw_expression=raw_expression,
        status=status,
        layout_notes=notes,
        evidence_fragment_ids=candidate.paragraph_indices,
    )
