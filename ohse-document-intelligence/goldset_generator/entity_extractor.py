"""Layer 3 entity extraction — cell-reference based, values derived from evidence."""

from __future__ import annotations

import re
from typing import Any

from goldset_generator.fact_resolver import resolve_field_display
from goldset_generator.provenance_builder import build_source_reference
from goldset_generator.table_schema_builder import build_page_schemas

SCHEMA_LABELS = frozenset(
    {
        "cas number",
        "cas",
        "twa",
        "stel",
        "ceiling",
        "exposure limit",
        "molecular weight",
        "health effect",
        "symbols",
        "نام علمی",
        "وزن مولکولی",
        "حد مجاز",
        "نماد",
        "ردیف",
        "مبنای",
    }
)

NUMERIC_ENTITY_TYPES = frozenset({"cas_number", "exposure_limit", "molecular_weight"})


def _is_schema_label(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if normalized in SCHEMA_LABELS:
        return True
    return any(label in normalized for label in SCHEMA_LABELS if len(label) >= 4)


def _entity(
    *,
    entity_type: str,
    cell_id: str | None,
    page_number: int,
    table_id: str | None,
    bbox: dict[str, Any] | None,
    confidence: float,
    document: str,
    field: str | None = None,
    display_text: str | None = None,
    resolved_value: str | None = None,
) -> dict[str, Any]:
    linked = [cell_id] if cell_id else []
    source_reference = build_source_reference(
        document=document,
        page_number=page_number,
        table_id=table_id,
        cell_id=cell_id,
        bbox=bbox,
    )
    payload: dict[str, Any] = {
        "type": entity_type,
        "confidence": confidence,
        "linked_cell_ids": linked,
        "source_reference": source_reference,
        "review_status": "pending",
    }
    if field:
        payload["field"] = field
        payload["value_reference"] = {"source": "cell", "cell_id": cell_id, "field": field}

    if entity_type in NUMERIC_ENTITY_TYPES:
        if resolved_value:
            payload["resolved_value"] = resolved_value
    elif display_text:
        payload["text"] = display_text
        if resolved_value:
            payload["resolved_value"] = resolved_value

    return payload


class EntityExtractor:
    """Extract entities deterministically from validated table gold schema."""

    def extract_from_table_golds(
        self,
        page_number: int,
        table_golds: list[dict[str, Any]],
        *,
        document: str = "OHE6.pdf",
    ) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        for schema in build_page_schemas(table_golds):
            if schema.get("page_number") != page_number:
                continue
            table_id = schema.get("table_id")

            for row in schema.get("rows") or []:
                subject = row.get("subject")
                fields = row.get("fields") or {}

                if subject and not _is_schema_label(subject):
                    key = ("chemical", subject.lower(), fields.get("chemical_name", {}).get("cell_id", ""))
                    if key not in seen:
                        seen.add(key)
                        chem_field = fields.get("persian_chemical_name") or fields.get("chemical_name") or {}
                        entities.append(
                            _entity(
                                entity_type="chemical",
                                cell_id=chem_field.get("cell_id"),
                                page_number=page_number,
                                table_id=table_id,
                                bbox=chem_field.get("bbox"),
                                confidence=0.92,
                                document=document,
                                field="chemical_name",
                                display_text=subject,
                                resolved_value=subject,
                            )
                        )

                cas_field = fields.get("CAS") or {}
                cas_value = resolve_field_display("CAS", cas_field)
                if cas_value and cas_field.get("cell_id"):
                    key = ("cas", cas_value, cas_field.get("cell_id", ""))
                    if key not in seen:
                        seen.add(key)
                        entities.append(
                            _entity(
                                entity_type="cas_number",
                                cell_id=cas_field.get("cell_id"),
                                page_number=page_number,
                                table_id=table_id,
                                bbox=cas_field.get("bbox"),
                                confidence=0.95,
                                document=document,
                                field="CAS",
                                resolved_value=cas_value,
                            )
                        )

                for limit_field in ("TWA", "STEL", "ceiling"):
                    field = fields.get(limit_field) or {}
                    limit_value = resolve_field_display(limit_field, field)
                    if limit_value and field.get("cell_id"):
                        key = ("limit", limit_field, field.get("cell_id", ""))
                        if key not in seen:
                            seen.add(key)
                            entities.append(
                                _entity(
                                    entity_type="exposure_limit",
                                    cell_id=field.get("cell_id"),
                                    page_number=page_number,
                                    table_id=table_id,
                                    bbox=field.get("bbox"),
                                    confidence=0.9,
                                    document=document,
                                    field=limit_field,
                                    resolved_value=limit_value,
                                )
                            )

                mw_field = fields.get("molecular_weight") or {}
                mw_value = resolve_field_display("molecular_weight", mw_field)
                if mw_value and mw_field.get("cell_id"):
                    key = ("mw", mw_value, mw_field.get("cell_id", ""))
                    if key not in seen:
                        seen.add(key)
                        entities.append(
                            _entity(
                                entity_type="molecular_weight",
                                cell_id=mw_field.get("cell_id"),
                                page_number=page_number,
                                table_id=table_id,
                                bbox=mw_field.get("bbox"),
                                confidence=0.88,
                                document=document,
                                field="molecular_weight",
                                resolved_value=mw_value,
                            )
                        )

        return entities

    def extract(
        self,
        page_number: int,
        page_text: str,
        cells: list[dict[str, Any]],
        tables: list[dict[str, Any]],
        *,
        table_golds: list[dict[str, Any]] | None = None,
        valid_cell_ids: list[str] | None = None,
        document: str = "OHE6.pdf",
    ) -> list[dict[str, Any]]:
        if table_golds:
            page_cell_ids = set(valid_cell_ids or [])
            entities = self.extract_from_table_golds(page_number, table_golds, document=document)
            if page_cell_ids:
                filtered: list[dict[str, Any]] = []
                for entity in entities:
                    linked = [cid for cid in entity.get("linked_cell_ids") or [] if cid in page_cell_ids]
                    if not linked:
                        continue
                    entity["linked_cell_ids"] = linked
                    entity["source_reference"]["cell_ids"] = linked
                    filtered.append(entity)
                return filtered
            return entities
        return []
