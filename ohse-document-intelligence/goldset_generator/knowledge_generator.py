"""Knowledge triple generation — deterministic from validated table schema."""

from __future__ import annotations

from typing import Any

from goldset_generator.fact_resolver import (
    PREDICATE_FIELD_MAP,
    cell_matches_predicate,
    predicate_for_field,
    resolve_field_display,
)
from goldset_generator.table_schema_builder import build_page_schemas


class KnowledgeGenerator:
    """Generate triples from table schema; Gemini never assigns object values."""

    def generate_from_table_golds(
        self,
        page_number: int,
        table_golds: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        triples: list[dict[str, Any]] = []

        for schema in build_page_schemas(table_golds):
            if schema.get("page_number") != page_number:
                continue

            for row in schema.get("rows") or []:
                subject = row.get("subject")
                if not subject:
                    continue

                for field_name in PREDICATE_FIELD_MAP.values():
                    field = (row.get("fields") or {}).get(field_name) or {}
                    cell_id = field.get("cell_id")
                    if not cell_id:
                        continue

                    value_status = field.get("value_status")
                    resolved = resolve_field_display(field_name, field)
                    if not resolved:
                        continue

                    predicate = predicate_for_field(field_name)
                    if not predicate:
                        continue

                    original = field.get("original_value") or resolved
                    if not cell_matches_predicate(predicate, original, resolved_value=resolved):
                        continue

                    triples.append(
                        {
                            "subject": subject,
                            "predicate": predicate,
                            "object_reference": {"cell_id": cell_id},
                            "semantic_confidence": 0.95 if value_status == "extracted" else 0.75,
                            "_field_name": field_name,
                            "_field_data": field,
                        }
                    )

        return triples

    def generate(
        self,
        page_number: int,
        page_text: str,
        cells: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        *,
        table_golds: list[dict[str, Any]] | None = None,
        valid_cell_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not table_golds:
            return []
        page_cell_ids = set(valid_cell_ids or [])
        triples = self.generate_from_table_golds(page_number, table_golds)
        if not page_cell_ids:
            return triples
        return [
            triple
            for triple in triples
            if (triple.get("object_reference") or {}).get("cell_id") in page_cell_ids
        ]

    def resolve_triples(
        self,
        triples: list[dict[str, Any]],
        cell_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        from goldset_generator.fact_resolver import resolve_object_reference

        resolved: list[dict[str, Any]] = []
        for triple in triples:
            item = dict(triple)
            field_name = item.pop("_field_name", None)
            field_data = item.pop("_field_data", None)

            object_text, source_ref = resolve_object_reference(
                triple.get("object_reference") or {},
                cell_by_id,
                field_data=field_data,
                field_name=field_name,
            )
            if not object_text or not source_ref:
                continue

            item["object"] = object_text
            item["source_reference"] = source_ref
            resolved.append(item)
        return resolved
