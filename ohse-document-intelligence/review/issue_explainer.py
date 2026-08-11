"""Human-readable explanations for machine validation issues.

The pipeline's validators emit terse, developer-oriented codes and messages
(e.g. ``VALUE_NOT_IN_EVIDENCE`` / "reference value not in document evidence:
field_name"). Those are correct for machines but not actionable for a human
reviewer. This module translates a raw issue dict into a plain-language
explanation plus a short, human-friendly title, without inventing any facts
beyond what the raw issue already asserts.

It also recognizes a handful of legacy free-text message patterns that were
written before issues were typed (from the pre-PostgreSQL JSON review queue)
and reclassifies them so they get a real title instead of "UNKNOWN".
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_VALUES = {"from_nearby_text", "field_name", "unknown", "n/a", "todo", "tbd"}

_ENTITY_CELL_RE = re.compile(r"^entity value not in linked cell:\s*'(.+)'$", re.I)
_TRIPLE_CELL_RE = re.compile(r"^triple object numeric not in source cell:\s*'(.+)'$", re.I)
_LOW_CONF_RE = re.compile(r"^final confidence below threshold:\s*([\d.]+)$", re.I)
_VALUE_NOT_IN_EVIDENCE_RE = re.compile(r"^reference value not in document evidence:\s*(.+)$", re.I)
_SYMBOL_NOT_TRACEABLE_RE = re.compile(r"^symbol not traceable to evidence:\s*(.+)$", re.I)

CODE_TITLES: dict[str, str] = {
    "VALUE_NOT_IN_EVIDENCE": "Value not confirmed in document",
    "UNIT_MISMATCH": "Missing measurement units",
    "COLUMN_AMBIGUOUS": "Ambiguous column mapping",
    "AMBIGUOUS_HEADER_MAPPING": "Ambiguous column mapping",
    "HEADER_UNKNOWN": "Unrecognized table header",
    "INCOMPLETE_HEADER_MAPPING": "Incomplete column mapping",
    "MERGED_CELL_UNRESOLVED": "Merged table cell",
    "CAS_INVALID": "Invalid CAS number",
    "ROW_ALIGNMENT_FAILED": "Row misaligned",
    "CROSS_PAGE_REFERENCE": "Reference spans multiple pages",
    "EXTRACTION_UNCERTAIN": "Extraction not fully reconstructed",
    "SCHEMA_UNKNOWN": "Unrecognized data schema",
    "NUMERIC_NORMALIZATION": "Numeric value needs review",
    "ENTITY_VALUE_MISMATCH": "Extracted value differs from table cell",
    "TRIPLE_VALUE_MISMATCH": "Extracted value differs from table cell",
    "LOW_CONFIDENCE": "Low extraction confidence",
    "UNKNOWN": "Needs review",
}


def human_issue_title(code: str) -> str:
    return CODE_TITLES.get((code or "").upper(), (code or "ISSUE").replace("_", " ").title())


def _reclassify_unknown(issue: dict[str, Any]) -> tuple[str, str]:
    """Best-effort re-derive a real code + explanation for legacy free-text
    issues (pre-typed JSON review queue) that only carry a message string."""
    message = str(issue.get("message") or "")

    m = _ENTITY_CELL_RE.match(message)
    if m:
        return "ENTITY_VALUE_MISMATCH", (
            f"The value '{m.group(1)}' was extracted as a standalone entity but does not match "
            f"the text of the table cell it is supposed to come from. This can happen when the "
            f"entity extractor picked up a number from a nearby row or column instead of the "
            f"correct cell. Check the PDF and confirm '{m.group(1)}' truly belongs to this row "
            f"before approving."
        )
    m = _TRIPLE_CELL_RE.match(message)
    if m:
        return "TRIPLE_VALUE_MISMATCH", (
            f"The numeric value '{m.group(1)}' produced by semantic extraction does not match "
            f"the source table cell's text. Verify against the PDF — the correct number may be "
            f"different, or this value may have leaked from an adjacent cell."
        )
    m = _LOW_CONF_RE.match(message)
    if m:
        return "LOW_CONFIDENCE", (
            f"The automatic extraction confidence for this page/item was low ({m.group(1)} out "
            f"of 1.0). Nothing is necessarily wrong, but please spot-check the values against "
            f"the PDF before approving."
        )
    if not message:
        return "UNKNOWN", "Unspecified issue — no message was recorded. Check the evidence manually."
    return "UNKNOWN", f"{message} (raw, untyped issue — please verify manually against the PDF)."


def explain_issue(issue: dict[str, Any]) -> str:
    """Return one full human-readable line for a single machine issue."""
    raw_code = str(issue.get("code") or issue.get("type") or "unknown").upper()
    severity = str(issue.get("severity", "medium")).lower()
    message = str(issue.get("message") or "")
    metadata = issue.get("metadata") or {}
    variable = metadata.get("variable")
    value = metadata.get("value")

    if raw_code in {"UNKNOWN", ""}:
        code, explanation = _reclassify_unknown(issue)
        return f"[{severity}] {human_issue_title(code)} — {explanation}"

    if raw_code == "VALUE_NOT_IN_EVIDENCE":
        sym_m = _SYMBOL_NOT_TRACEABLE_RE.match(message)
        if sym_m:
            symbol = metadata.get("symbol") or sym_m.group(1)
            return (
                f"[{severity}] Symbol not found in document — The symbol '{symbol}' appears in "
                f"the reconstructed formula expression but could not be found anywhere in the "
                f"nearby document text used as evidence. Check the PDF: either the symbol is "
                f"written differently there (e.g. subscript, different spelling), or the formula "
                f"reconstruction introduced a symbol that isn't actually in the source."
            )
        m = _VALUE_NOT_IN_EVIDENCE_RE.match(message)
        val = value if value is not None else (m.group(1) if m else None)
        if val and str(val).lower() in _PLACEHOLDER_VALUES:
            who = f" for variable '{variable}'" if variable else ""
            return (
                f"[{severity}] {human_issue_title(raw_code)} — The extraction left an unresolved "
                f"placeholder ('{val}'){who} instead of a real value from the PDF. This means the "
                f"model could not find the actual number, not just that it is unverified. Open the "
                f"PDF evidence, find the real value if present, and enter it via CORRECT — or REJECT "
                f"if it cannot be determined from this page."
            )
        target = f"'{val}'" if val else "this value"
        who = f" (variable '{variable}')" if variable else ""
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {target}{who} does not appear in the "
            f"extracted document text near this item. Open the PDF evidence panel and confirm "
            f"whether it is actually present nearby; if so this may just be an OCR/text-matching "
            f"gap, if not the extracted value may be wrong."
        )

    if raw_code == "UNIT_MISMATCH":
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {message}. This is a warning only and "
            f"does not block approval, but check the PDF for units (e.g. ppm, mg/m³, s) near the "
            f"variables and add them via CORRECT if visible."
        )

    if raw_code in {"COLUMN_AMBIGUOUS", "AMBIGUOUS_HEADER_MAPPING"}:
        fields = metadata.get("fields") or issue.get("fields")
        extra = f" Candidate fields: {fields}." if fields else ""
        return (
            f"[{severity}] {human_issue_title(raw_code)} — Two or more table columns could map to "
            f"the same field.{extra} Open the PDF header row and confirm the correct "
            f"column-to-field mapping via CORRECT."
        )

    if raw_code in {"HEADER_UNKNOWN", "INCOMPLETE_HEADER_MAPPING"}:
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {message}. Check the PDF table header "
            f"row and map any missing or unrecognized columns via CORRECT."
        )

    if raw_code == "MERGED_CELL_UNRESOLVED":
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {message}. This cell spans a merged "
            f"region in the PDF table. Verify how the merged content should be split across "
            f"rows/columns and correct the affected cell values if needed."
        )

    if raw_code == "CAS_INVALID":
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {message}. Check the CAS registry "
            f"number against the PDF; it may be an OCR digit error."
        )

    if raw_code == "EXTRACTION_UNCERTAIN":
        m = _SYMBOL_NOT_TRACEABLE_RE.match(message)
        if m:
            return (
                f"[{severity}] {human_issue_title(raw_code)} — The symbol '{m.group(1)}' in the "
                f"reconstructed formula could not be found anywhere in the nearby document text. "
                f"Verify the formula reconstruction against the PDF."
            )
        field_name = issue.get("field") or metadata.get("field")
        if field_name in {"TWA", "STEL", "ceiling"}:
            return (
                f"[{severity}] Exposure limit value unreadable ({field_name}) — The source cell "
                f"clearly contains a {field_name} value (it has a unit like ppm/mg/m³), but the "
                f"digits could not be read cleanly — often because the PDF encodes them in a "
                f"decorative/math font that OCR misreads as symbols (e.g. \\cdot, \\Delta, \\pi). "
                f"Open the PDF evidence for this row, read the {field_name} value directly, and "
                f"enter it via CORRECT."
            )
        return (
            f"[{severity}] {human_issue_title(raw_code)} — {message}. The system could not fully "
            f"reconstruct this item from the PDF layout; please verify it manually."
        )

    if raw_code == "NUMERIC_NORMALIZATION":
        return f"[{severity}] {human_issue_title(raw_code)} — {message}. Check this number against the PDF."

    return f"[{severity}] {human_issue_title(raw_code)} — {message}"


def dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate issues (same code/type + message + metadata), preserving
    order. The legacy JSON review queue is append-only across pipeline reruns and
    often accumulates the same issue several times."""
    import json as _json

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for issue in issues:
        key = _json.dumps(
            [
                str(issue.get("code") or issue.get("type") or ""),
                str(issue.get("message") or ""),
                issue.get("metadata") or {},
            ],
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
