"""Validation orchestrator — typed errors before domain persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from goldset_generator.fact_resolver import resolve_field_display
from pipeline_contracts.confidence import LayeredConfidence
from pipeline_contracts.validation_codes import ValidationErrorCode, ValidationIssue
from schema_registry.registry import get_schema_registry
from validation_engine.rules.cas_rules import validate_cas
from validation_engine.rules.exposure_rules import validate_exposure


@dataclass
class ValidationResult:
    passed: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    requires_review: bool = False
    confidence: LayeredConfidence = field(default_factory=LayeredConfidence)

    @property
    def legacy_issues(self) -> list[str]:
        return [issue.to_legacy_string() for issue in self.issues]


class ValidationEngine:
    def __init__(self) -> None:
        self.registry = get_schema_registry()

    def validate_table_row(
        self,
        table_type: str,
        row: dict[str, Any],
        *,
        schema_id: str | None = None,
    ) -> ValidationResult:
        schema = self.registry.get(schema_id) if schema_id else self.registry.for_table_type(table_type)
        if not schema:
            return ValidationResult(
                passed=True,
                confidence=LayeredConfidence(validation=0.5),
                issues=[
                    ValidationIssue(
                        code=ValidationErrorCode.SCHEMA_UNKNOWN,
                        message=f"No schema registered for table_type={table_type}",
                        severity="warning",
                    )
                ],
            )

        issues: list[ValidationIssue] = []
        requires_review = False

        for field_def in schema.get("fields", []):
            field_name = field_def["name"]
            field_data = row.get(field_name)
            if not isinstance(field_data, dict):
                continue

            status = field_data.get("value_status")
            if status == "merged_cell":
                requires_review = True
                issues.append(
                    ValidationIssue(
                        code=ValidationErrorCode.MERGED_CELL_UNRESOLVED,
                        message="merged cell requires human review",
                        field_name=field_name,
                        severity="warning",
                    )
                )
                continue

            value = resolve_field_display(field_name, field_data)
            validation = field_def.get("validation")

            if validation == "cas_format" and value:
                for msg in validate_cas(value):
                    issues.append(
                        ValidationIssue(
                            code=ValidationErrorCode.CAS_INVALID,
                            message=msg,
                            field_name=field_name,
                        )
                    )
            if validation == "exposure_unit" and value:
                for msg in validate_exposure(value, field_data.get("unit")):
                    issues.append(
                        ValidationIssue(
                            code=ValidationErrorCode.UNIT_MISMATCH,
                            message=msg,
                            field_name=field_name,
                        )
                    )

            if status == "extraction_uncertain":
                requires_review = True
                issues.append(
                    ValidationIssue(
                        code=ValidationErrorCode.EXTRACTION_UNCERTAIN,
                        message="field extraction marked uncertain",
                        field_name=field_name,
                        severity="warning",
                    )
                )

        hard_fail = any(issue.severity == "error" for issue in issues)
        passed = not hard_fail
        validation_score = 1.0 if passed and not requires_review else (0.7 if passed else 0.0)

        return ValidationResult(
            passed=passed,
            issues=issues,
            requires_review=requires_review,
            confidence=LayeredConfidence(validation=validation_score),
        )
