"""Strict formula validation — evidence traceability over domain knowledge."""

from __future__ import annotations

import re
from typing import Any

from pipeline_contracts.validation_codes import ValidationErrorCode, ValidationIssue

SYMBOL_PATTERN = re.compile(r"[A-Za-z]+\(\d+\)|[a-z]{2,5}(?:_\d+|\^?\d+)?", re.I)
NUMERIC_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


class FormulaValidator:
    def validate(
        self,
        gold: dict[str, Any],
        page_text: str,
        *,
        reconstruction_status: str,
    ) -> dict[str, Any]:
        issues: list[ValidationIssue] = []
        evidence_text = self._collect_evidence_text(gold, page_text)
        expression = (
            gold.get("reconstruction", {}).get("expression")
            or gold.get("normalized_expression")
            or ""
        )

        structural = reconstruction_status == "validated" and bool(expression) and expression not in {
            "√",
            "sqrt",
            "ahv=√",
            "ahv = sqrt",
        }
        if not structural:
            issues.append(
                ValidationIssue(
                    code=ValidationErrorCode.EXTRACTION_UNCERTAIN,
                    message="formula not structurally reconstructed",
                    field_name="reconstruction.expression",
                )
            )

        symbol_traceable = self._validate_symbols(expression, evidence_text, issues)
        numeric_traceable = self._validate_reference_values(gold, evidence_text, issues)
        unit_consistent = self._validate_units(gold, issues)

        if structural and symbol_traceable and numeric_traceable and unit_consistent:
            overall = "approved"
        elif issues:
            overall = "review_required"
        else:
            overall = "extraction_uncertain"

        return {
            "structural": structural,
            "symbol_traceability": symbol_traceable,
            "numeric_traceability": numeric_traceable,
            "unit_consistency": unit_consistent,
            "issues": [issue.to_dict() for issue in issues],
            "overall": overall,
        }

    @staticmethod
    def _collect_evidence_text(gold: dict[str, Any], page_text: str) -> str:
        parts = [page_text or ""]
        evidence = gold.get("evidence") or {}
        parts.extend(evidence.get("raw_text_fragments") or [])
        parts.append(gold.get("evidence", {}).get("candidate_text") or "")
        for fragment in evidence.get("fragments") or []:
            if isinstance(fragment, dict):
                parts.append(fragment.get("text") or "")
            elif isinstance(fragment, str):
                parts.append(fragment)
        return "\n".join(parts)

    def _validate_symbols(
        self,
        expression: str,
        evidence_text: str,
        issues: list[ValidationIssue],
    ) -> bool:
        if not expression:
            return False
        evidence_lower = evidence_text.lower()
        ok = True
        for symbol in SYMBOL_PATTERN.findall(expression):
            base = re.sub(r"[_^\d]+", "", symbol).lower()
            if base in {"sqrt", "sum"}:
                continue
            if len(base) <= 1 and base not in {"t"}:
                continue
            if base not in evidence_lower and symbol.lower() not in evidence_lower:
                issues.append(
                    ValidationIssue(
                        code=ValidationErrorCode.VALUE_NOT_IN_EVIDENCE,
                        message=f"symbol not traceable to evidence: {symbol}",
                        field_name="reconstruction.expression",
                        metadata={"symbol": symbol},
                    )
                )
                ok = False
        return ok

    def _validate_reference_values(
        self,
        gold: dict[str, Any],
        evidence_text: str,
        issues: list[ValidationIssue],
    ) -> bool:
        semantics = gold.get("semantics") or {}
        reference_values = semantics.get("reference_values") or []
        ok = True
        for ref in reference_values:
            value = str(ref.get("reference_value") or "")
            if not value:
                continue
            if value not in evidence_text and not self._numeric_in_evidence(value, evidence_text):
                issues.append(
                    ValidationIssue(
                        code=ValidationErrorCode.VALUE_NOT_IN_EVIDENCE,
                        message=f"reference value not in document evidence: {value}",
                        field_name="semantics.reference_values",
                        metadata={"variable": ref.get("variable"), "value": value},
                    )
                )
                ok = False
        return ok

    @staticmethod
    def _numeric_in_evidence(value: str, evidence_text: str) -> bool:
        return value in evidence_text.replace(" ", "")

    @staticmethod
    def _validate_units(gold: dict[str, Any], issues: list[ValidationIssue]) -> bool:
        semantics = gold.get("semantics") or {}
        variables = semantics.get("variables") or {}
        units = semantics.get("units") or {}
        if variables and not units:
            issues.append(
                ValidationIssue(
                    code=ValidationErrorCode.UNIT_MISMATCH,
                    message="variables present but units missing",
                    field_name="semantics.units",
                    severity="warning",
                )
            )
            return True  # warning only — do not block approval
        return True
