"""Hard quality gate before table gold promotion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline_contracts.header_reconstruction import HeaderStructure
from pipeline_contracts.numeric_integrity import validate_row_number_cell


@dataclass
class TableQualityResult:
    gold_allowed: bool
    geometry_valid: bool
    column_count_valid: bool
    header_structure_valid: bool
    merged_cells_valid: bool
    numeric_integrity_valid: bool
    provenance_complete: bool
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gold_allowed": self.gold_allowed,
            "geometry_valid": self.geometry_valid,
            "column_count_valid": self.column_count_valid,
            "header_structure_valid": self.header_structure_valid,
            "merged_cells_valid": self.merged_cells_valid,
            "numeric_integrity_valid": self.numeric_integrity_valid,
            "provenance_complete": self.provenance_complete,
            "issues": self.issues,
        }


def evaluate_table_quality(
    *,
    header_structure: HeaderStructure,
    table_gold: dict[str, Any],
    numeric_issues: list[dict[str, Any]] | None = None,
) -> TableQualityResult:
    issues: list[dict[str, Any]] = list(header_structure.issues)
    if numeric_issues:
        issues.extend(numeric_issues)

    geometry_valid = header_structure.geometry_valid
    column_count_valid = not any(i.get("type") == "column_count_mismatch" for i in issues)
    header_structure_valid = header_structure.header_structure_valid

    merged_cells_valid = True
    for row in table_gold.get("rows") or []:
        cas_numbers = row.get("cas_numbers")
        has_resolved_cas = isinstance(cas_numbers, list) and len(cas_numbers) > 0
        for field_name, field_data in row.items():
            if not isinstance(field_data, dict):
                continue
            if field_data.get("value_status") != "merged_cell":
                continue
            if field_name == "chemical_name" and has_resolved_cas:
                continue
            merged_cells_valid = False
            issues.append(
                {
                    "type": "merged_cell_unresolved",
                    "severity": "medium",
                    "message": f"unresolved merged cell in field {field_name}",
                    "field": field_name,
                }
            )

    numeric_integrity_valid = not any(
        i.get("type") == "NUMERIC_VALUE_MISMATCH" for i in (numeric_issues or [])
    )

    provenance_complete = True
    for row in table_gold.get("rows") or []:
        cas_numbers = row.get("cas_numbers")
        if isinstance(cas_numbers, list):
            for idx, cas_entry in enumerate(cas_numbers):
                if not isinstance(cas_entry, dict):
                    continue
                if cas_entry.get("value") and not cas_entry.get("source_cell_id"):
                    provenance_complete = False
                    issues.append(
                        {
                            "type": "missing_provenance",
                            "severity": "medium",
                            "message": f"cas_numbers[{idx}] missing source_cell_id",
                            "field": "cas_numbers",
                        }
                    )
        for field_name, field_data in row.items():
            if not isinstance(field_data, dict):
                continue
            if field_data.get("value") is None:
                continue
            ref = field_data.get("source_reference") or {}
            if not ref.get("cell_ids") and not field_data.get("cell_id"):
                provenance_complete = False
                issues.append(
                    {
                        "type": "missing_provenance",
                        "severity": "medium",
                        "message": f"field {field_name} missing cell provenance",
                        "field": field_name,
                    }
                )

    # Row-number integrity across all rows.
    for row in table_gold.get("rows") or []:
        rn = row.get("row_number")
        if not isinstance(rn, dict):
            continue
        orig = rn.get("original_value") or rn.get("value") or ""
        for msg in validate_row_number_cell(str(orig)):
            issues.append(
                {
                    "type": "ROW_NUMBER_MULTI_VALUE",
                    "severity": "critical",
                    "message": msg,
                    "field": "row_number",
                }
            )
            numeric_integrity_valid = False

    gold_allowed = (
        geometry_valid
        and column_count_valid
        and header_structure_valid
        and merged_cells_valid
        and numeric_integrity_valid
        and provenance_complete
    )

    return TableQualityResult(
        gold_allowed=gold_allowed,
        geometry_valid=geometry_valid,
        column_count_valid=column_count_valid,
        header_structure_valid=header_structure_valid,
        merged_cells_valid=merged_cells_valid,
        numeric_integrity_valid=numeric_integrity_valid,
        provenance_complete=provenance_complete,
        issues=issues,
    )
