"""Deterministic numeric integrity contract for table cells.

Invariant:
  physical cell text → original_value (exact source) → parsed_token → normalized_value

The LLM must never be the authority for numerical values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PERSIAN_DIGIT = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
UNIT_PATTERN = re.compile(r"\b(ppm|mg/m³|mg/m3|f/ml)\b", re.IGNORECASE)
NUMERIC_TOKEN = re.compile(r"[\d۰-۹]+(?:[./][\d۰-۹]+)?")
SLASH_TOKEN = re.compile(r"[\d۰-۹]+\s*/\s*[\d۰-۹]+")
CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")
# Incomplete mg/m (no cubic yet). A lone 3/³ beside it is the unit exponent, not the limit.
_INCOMPLETE_MGM = re.compile(r"mg/m(?![3³^])", re.IGNORECASE)
_RTL_CUBIC_THREE = re.compile(
    r"(?<![\d۰-۹.])([3۳³])\s*(mg/m)(?![3³^])",
    re.IGNORECASE,
)
_LTR_CUBIC_THREE = re.compile(
    r"(mg/m)(?![3³^])\s*([3۳³])(?![\d۰-۹])",
    re.IGNORECASE,
)
_MGM_CUBIC_UNIT = re.compile(
    r"mg\s*/\s*m(?:³|3|\^\s*\{\s*3(?:\s*\([^)]*\))*\s*\}|\^\s*3)",
    re.IGNORECASE,
)
_UNIT_QUALIFIER = re.compile(
    r"\(\s*(?:I|E|R|IFV|IV|F)\s*\)",
    re.IGNORECASE,
)


@dataclass
class NumericCellResult:
    """original_value is always the full physical cell text; parsed_token is the primary numeric."""

    original_value: str | None
    parsed_token: str | None
    normalized_value: str | None
    numeric_parse_status: str  # VALIDATED | REVIEW_REQUIRED | NOT_NUMERIC | ABSENT
    numeric_parse_method: str | None
    unit: str | None = None

    def to_field_metadata(self) -> dict[str, Any]:
        return {
            "original_value": self.original_value,
            "normalized_value": self.normalized_value,
            "numeric_parse_status": self.numeric_parse_status,
            "numeric_parse_method": self.numeric_parse_method,
        }


def _translate_digits(text: str) -> str:
    return (text or "").translate(PERSIAN_DIGIT)


def parse_slash_decimal(numerator: str, denominator: str) -> str | None:
    """Deterministic OCR slash-decimal: ``3/0`` → ``0.3``, ``0009/0`` → ``0.0009``."""
    num = _translate_digits(numerator.strip())
    den = _translate_digits(denominator.strip())
    if den == "0" and num:
        return f"0.{num}"
    if num == "0" and den.isdigit():
        return f"0.{den}"
    if len(num) <= 2 and len(den) == 1:
        return f"{num}.{den}"
    return None


def try_normalize(token: str, *, field_type: str = "generic") -> tuple[str | None, str | None]:
    """Return (normalized_value, method) only when conversion is deterministically safe."""
    if not token or token.strip() in {"-", "—", "–"}:
        return None, None

    ascii_token = _translate_digits(token.strip())

    if re.fullmatch(r"\d+\.\d+", ascii_token):
        return ascii_token, "plain_decimal"

    if field_type == "molecular_weight":
        mw_match = re.fullmatch(r"(\d{1,3})/(\d{2,4})", ascii_token)
        if mw_match:
            whole, frac = mw_match.groups()
            if len(frac) == 2:
                return f"{whole}.{frac}", "molecular_weight_slash"
            return f"{frac}.{whole.zfill(2)}", "molecular_weight_slash"

    if "/" in token:
        parts = token.split("/", 1)
        if len(parts) == 2:
            parsed = parse_slash_decimal(parts[0], parts[1])
            if parsed:
                return parsed, "deterministic_persian_decimal"

    if re.fullmatch(r"[\d۰-۹]+", token.strip()):
        return ascii_token, "digit_translation"

    return None, None


def _collapse_latex_cubic_metre(text: str) -> str:
    return re.sub(
        r"m\s*\^\s*\{\s*3(?:\s*\([^)]*\))*\s*\}",
        "m³",
        text,
        flags=re.IGNORECASE,
    )


def _collapse_mg_m_unit_exponent(text: str) -> str:
    """Treat a lone 3/³ beside incomplete ``mg/m`` as m³ when another number remains."""
    if not _INCOMPLETE_MGM.search(text):
        return text

    for pattern in (_RTL_CUBIC_THREE, _LTR_CUBIC_THREE):
        candidate, count = pattern.subn(r"mg/m³", text, count=1)
        if count and NUMERIC_TOKEN.search(candidate):
            return candidate
    return text


def _is_mg_m3_unit_only(text: str) -> bool:
    """True when the cell is a cubic-metre unit (plus optional I/E-style tags) with no exposure value."""
    if not _MGM_CUBIC_UNIT.search(text):
        return False
    remainder = _MGM_CUBIC_UNIT.sub(" ", text)
    remainder = _UNIT_QUALIFIER.sub(" ", remainder)
    if SLASH_TOKEN.search(remainder):
        return False
    return NUMERIC_TOKEN.search(remainder) is None


def extract_primary_numeric_token(text: str, *, field_type: str = "generic") -> str | None:
    """Extract the primary numeric token from cell text without reinterpretation."""
    if not text or not text.strip():
        return None

    value_text = _collapse_latex_cubic_metre(text)

    if field_type == "molecular_weight":
        mw = re.search(r"(\d{1,3}|[\d۰-۹]{1,3})\s*/\s*(\d{2,4}|[\d۰-۹]{2,4})", value_text)
        if mw:
            return mw.group(0).strip()

    if field_type in {"TWA", "STEL", "ceiling"}:
        if _is_mg_m3_unit_only(value_text):
            return None
        value_text = _collapse_mg_m_unit_exponent(value_text)
        slash_tokens = SLASH_TOKEN.findall(value_text)
        if slash_tokens:
            return slash_tokens[-1]
        unit_match = UNIT_PATTERN.search(value_text)
        if unit_match:
            before = value_text[: unit_match.start()]
            tokens = NUMERIC_TOKEN.findall(before)
            if tokens:
                return tokens[-1]
        tokens = NUMERIC_TOKEN.findall(value_text)
        if tokens:
            return tokens[0]

    if field_type == "row_number":
        tokens = NUMERIC_TOKEN.findall(value_text)
        return tokens[0] if tokens else None

    match = NUMERIC_TOKEN.search(value_text)
    return match.group(0) if match else None


def extract_ceiling_from_stel_c_cell(text: str) -> str | None:
    """Extract ceiling notation (C …) exactly as represented in a STEL/C column cell."""
    if not text or not re.search(r"\bC\b", text, re.IGNORECASE):
        return None

    compact = _translate_digits(text.replace("\n", " "))

    slash_after_c = re.search(r"C\s*(?:ppm\s*)?(\d+)\s*/\s*(\d+)", compact, re.IGNORECASE)
    if slash_after_c:
        num, den = slash_after_c.group(1), slash_after_c.group(2)
        for pattern in (f"{num}/{den}", f"{num} /{den}", f"{num}/ {den}", f"{num} / {den}"):
            if pattern in _translate_digits(text) or pattern.replace(" ", "") in compact.replace(" ", ""):
                unit = " ppm" if re.search(r"ppm", text, re.I) else ""
                return f"C {num}/{den}{unit}".strip()
        unit = " ppm" if re.search(r"ppm", text, re.I) else ""
        return f"C {num}/{den}{unit}".strip()

    inline = re.search(r"C\s*(\d+(?:/\d+)?)", compact, re.IGNORECASE)
    if inline:
        return inline.group(0).strip()

    return None


def parse_numeric_cell(
    text: str,
    *,
    field_type: str = "generic",
    allow_normalization: bool = True,
) -> NumericCellResult:
    """Parse a numeric table cell — original_value is always the full source cell text."""
    source_text = (text or "").strip()
    if not source_text or source_text in {"-", "—", "–"}:
        return NumericCellResult(
            original_value=None,
            parsed_token=None,
            normalized_value=None,
            numeric_parse_status="ABSENT",
            numeric_parse_method=None,
        )

    parsed_token = extract_primary_numeric_token(source_text, field_type=field_type)
    if not parsed_token:
        return NumericCellResult(
            original_value=source_text,
            parsed_token=None,
            normalized_value=None,
            numeric_parse_status="NOT_NUMERIC",
            numeric_parse_method=None,
        )

    unit_match = UNIT_PATTERN.search(source_text)
    unit = unit_match.group(1) if unit_match else None

    normalized: str | None = None
    method: str | None = None
    status = "VALIDATED"

    if allow_normalization:
        normalized, method = try_normalize(parsed_token, field_type=field_type)
        if normalized is None:
            status = "REVIEW_REQUIRED"
            normalized = _translate_digits(parsed_token)
            method = "digit_translation_only"
        elif normalized == _translate_digits(parsed_token):
            method = method or "identity"

    return NumericCellResult(
        original_value=source_text,
        parsed_token=parsed_token,
        normalized_value=normalized,
        numeric_parse_status=status,
        numeric_parse_method=method,
        unit=unit,
    )


def _compact_numeric(text: str) -> str:
    return re.sub(r"\s+", "", _translate_digits(text or ""))


def numeric_value_mismatch(original: str | None, candidate_value: str | None, source_text: str) -> bool:
    """True when candidate numeric cannot be proven from source evidence."""
    if not candidate_value or not source_text:
        return False

    source_ascii = _translate_digits(source_text)
    source_compact = _compact_numeric(source_text)
    cand_ascii = _translate_digits(str(candidate_value))
    cand_compact = _compact_numeric(str(candidate_value))

    if cand_compact in source_compact or cand_ascii in source_ascii:
        return False

    norm, method = try_normalize(str(candidate_value), field_type="molecular_weight")
    if norm and norm == cand_ascii and method and cand_compact in source_compact:
        return False

    return True


def validate_row_number_cell(text: str) -> list[str]:
    """Detect multiple row numbers merged into one cell."""
    if not text:
        return []
    nums = re.findall(r"[\d۰-۹]{1,4}", text)
    unique = []
    for n in nums:
        if n not in unique:
            unique.append(n)
    if len(unique) > 1:
        return [f"ROW_NUMBER_MULTI_VALUE: {unique!r} in {text!r}"]
    return []
