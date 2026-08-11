"""Build validated semantic table schema from table gold before Gemini."""

from __future__ import annotations

from typing import Any

from goldset_generator.fact_resolver import resolve_field_display

CHEMICAL_ROW_FIELDS = (
    "row_number",
    "chemical_name",
    "persian_chemical_name",
    "CAS",
    "molecular_weight",
    "TWA",
    "STEL",
    "ceiling",
    "symbols",
    "health_effect",
)


def _row_subject(row: dict[str, Any]) -> str | None:
    for key in ("persian_chemical_name", "chemical_name"):
        field = row.get(key) or {}
        if field.get("value_status") == "merged_cell":
            continue
        value = field.get("value")
        if value and str(value).strip():
            return str(value).strip()
    chem = row.get("chemical_name") or {}
    if chem.get("original_value"):
        from goldset_generator.table_gold_generator import _clean_chemical_name

        cleaned = _clean_chemical_name(chem["original_value"])
        if cleaned:
            return cleaned
    return None


def build_table_schema(table_gold: dict[str, Any]) -> dict[str, Any]:
    """Convert validated table gold into row/column semantic schema for Layer 3."""
    schema_rows: list[dict[str, Any]] = []

    for row_index, row in enumerate(table_gold.get("rows") or [], start=1):
        subject = _row_subject(row)
        if not subject:
            continue

        fields: dict[str, Any] = {}
        for field_name in CHEMICAL_ROW_FIELDS:
            field_data = row.get(field_name)
            if not isinstance(field_data, dict):
                continue
            display = resolve_field_display(field_name, field_data)
            fields[field_name] = {
                "cell_id": field_data.get("cell_id"),
                "value": display,
                "unit": field_data.get("unit"),
                "value_status": field_data.get("value_status"),
                "original_value": field_data.get("original_value"),
                "bbox": field_data.get("bbox"),
                "source_reference": field_data.get("source_reference"),
            }

        chem_field = fields.get("chemical_name") or {}
        if chem_field.get("value_status") == "merged_cell" and not (fields.get("CAS") or {}).get("value"):
            cas_value = resolve_field_display("CAS", chem_field)
            if cas_value:
                fields["CAS"] = {
                    "cell_id": chem_field.get("cell_id"),
                    "value": cas_value,
                    "unit": None,
                    "value_status": "merged_cell",
                    "original_value": chem_field.get("original_value"),
                    "bbox": chem_field.get("bbox"),
                    "source_reference": chem_field.get("source_reference"),
                }

        schema_rows.append(
            {
                "row_index": row_index,
                "subject": subject,
                "fields": fields,
            }
        )

    return {
        "table_id": table_gold.get("table_id"),
        "page_number": table_gold.get("page_number"),
        "table_type": table_gold.get("table_type"),
        "header_mapping": table_gold.get("header_mapping") or {},
        "rows": schema_rows,
    }


def build_page_schemas(table_golds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [build_table_schema(table) for table in table_golds if table.get("rows")]
