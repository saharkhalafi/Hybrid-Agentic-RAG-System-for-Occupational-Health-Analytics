"""Shared table pipeline diagnostics (used by all-pages pipeline + tests)."""

from __future__ import annotations

from typing import Any

from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator
from pipeline_contracts.header_reconstruction import reconstruct_header_structure


def _cell_detail(cell: dict[str, Any]) -> dict[str, Any]:
    ref = cell.get("source_reference") or {}
    return {
        "cell_id": cell.get("cell_id"),
        "row": cell.get("row"),
        "column": cell.get("column"),
        "original_value": cell.get("text"),
        "bbox": cell.get("bbox"),
        "bbox_source": cell.get("bbox_source"),
        "row_span": ref.get("row_span", 1),
        "column_span": ref.get("column_span", 1),
        "source": cell.get("source"),
        "column_name": ref.get("column_name"),
        "value_status": ref.get("value_status"),
        "page_number": cell.get("page_number"),
    }


def _gold_field_detail(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, data in row.items():
        if not isinstance(data, dict):
            out[field] = data
            continue
        out[field] = {
            "original_value": data.get("original_value"),
            "value": data.get("value"),
            "normalized_value": data.get("normalized_value"),
            "numeric_parse_status": data.get("numeric_parse_status"),
            "value_status": data.get("value_status"),
            "cell_id": data.get("cell_id"),
            "bbox": (data.get("source_reference") or {}).get("bbox"),
        }
    return out


def diagnose_table(table: dict[str, Any], *, recovery_method: str | None) -> dict[str, Any]:
    rows = table.get("rows") or []
    header_structure = reconstruct_header_structure(
        rows,
        table_type=table.get("table_type", "chemical_oel"),
    )
    gold = TableGoldGenerator().generate(table)
    all_cells = [c for row in rows for c in row if isinstance(c, dict)]
    validator = GoldsetValidator(all_cells)
    numeric_issues: list[dict[str, Any]] = []
    for row in gold.get("rows") or []:
        for field_name, field_data in row.items():
            if isinstance(field_data, dict):
                for issue in validator.validate_table_field(field_name, field_data):
                    numeric_issues.append({
                        "field": field_name,
                        "issue": issue,
                        "severity": "CRITICAL" if "CRITICAL" in issue else "HIGH",
                    })

    review_required = not gold.get("gold_allowed", False) or bool(numeric_issues)
    issues: list[str] = []
    if header_structure.physical_column_count != 7:
        issues.append(f"column_count={header_structure.physical_column_count}")
    if not header_structure.header_structure_valid:
        issues.append("header_structure_invalid")
    if numeric_issues:
        issues.append("numeric_mismatch")
    if any(i.get("type") == "contaminated_header_row" for i in header_structure.issues):
        issues.append("contaminated_header")

    return {
        "table_id": table.get("table_id"),
        "page_number": table.get("page_number"),
        "recovery_method": recovery_method,
        "structural_confidence": table.get("structural_confidence"),
        "physical_row_count": len(rows),
        "physical_column_count": header_structure.physical_column_count,
        "data_row_start": header_structure.data_row_start,
        "header_tree": [
            {
                "physical_column_id": col.physical_column_id,
                "column_index": col.column_index,
                "header_path": col.header_path,
                "semantic_field": col.semantic_field,
                "parent_header": col.parent_header,
            }
            for col in header_structure.physical_columns
        ],
        "header_mapping": gold.get("header_mapping"),
        "reconstructed_cells": [_cell_detail(c) for c in all_cells],
        "sample_extracted_rows": [_gold_field_detail(r) for r in (gold.get("rows") or [])[:3]],
        "extracted_row_count": len(gold.get("rows") or []),
        "numeric_validation": numeric_issues,
        "gold_allowed": gold.get("gold_allowed", False),
        "review_required": review_required,
        "mapping_status": gold.get("mapping_status"),
        "table_quality": gold.get("table_quality"),
        "structure_issues": header_structure.issues,
        "validation_issues": issues,
    }
