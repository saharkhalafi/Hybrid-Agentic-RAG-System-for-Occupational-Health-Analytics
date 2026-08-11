"""Typed validation errors for monitoring and review routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ValidationErrorCode(str, Enum):
    UNIT_MISMATCH = "UNIT_MISMATCH"
    COLUMN_AMBIGUOUS = "COLUMN_AMBIGUOUS"
    MERGED_CELL_UNRESOLVED = "MERGED_CELL_UNRESOLVED"
    CAS_INVALID = "CAS_INVALID"
    HEADER_UNKNOWN = "HEADER_UNKNOWN"
    ROW_ALIGNMENT_FAILED = "ROW_ALIGNMENT_FAILED"
    VALUE_NOT_IN_EVIDENCE = "VALUE_NOT_IN_EVIDENCE"
    CROSS_PAGE_REFERENCE = "CROSS_PAGE_REFERENCE"
    EXTRACTION_UNCERTAIN = "EXTRACTION_UNCERTAIN"
    SCHEMA_UNKNOWN = "SCHEMA_UNKNOWN"
    NUMERIC_NORMALIZATION = "NUMERIC_NORMALIZATION"
    NUMERIC_VALUE_MISMATCH = "NUMERIC_VALUE_MISMATCH"
    ROW_NUMBER_MULTI_VALUE = "ROW_NUMBER_MULTI_VALUE"


@dataclass
class ValidationIssue:
    code: ValidationErrorCode
    message: str
    field_name: str | None = None
    severity: str = "error"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "severity": self.severity,
        }
        if self.field_name:
            payload["field"] = self.field_name
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    def to_legacy_string(self) -> str:
        if self.field_name:
            return f"{self.field_name}: {self.message}"
        return self.message

    @classmethod
    def from_legacy_string(cls, text: str, *, field: str | None = None) -> ValidationIssue:
        lowered = text.lower()
        if "merged_cell" in lowered:
            code = ValidationErrorCode.MERGED_CELL_UNRESOLVED
        elif "cas" in lowered and "invalid" in lowered:
            code = ValidationErrorCode.CAS_INVALID
        elif "unit" in lowered:
            code = ValidationErrorCode.UNIT_MISMATCH
        elif "page" in lowered and "cross" in lowered:
            code = ValidationErrorCode.CROSS_PAGE_REFERENCE
        elif "evidence" in lowered:
            code = ValidationErrorCode.VALUE_NOT_IN_EVIDENCE
        elif "header" in lowered or "schema label" in lowered:
            code = ValidationErrorCode.HEADER_UNKNOWN
        elif "alignment" in lowered:
            code = ValidationErrorCode.ROW_ALIGNMENT_FAILED
        elif "ambiguous" in lowered:
            code = ValidationErrorCode.COLUMN_AMBIGUOUS
        else:
            code = ValidationErrorCode.NUMERIC_NORMALIZATION
        return cls(code=code, message=text, field_name=field)
