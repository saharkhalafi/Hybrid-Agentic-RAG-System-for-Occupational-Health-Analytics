"""QA benchmark generation — reference-based answers, not Gemini-generated strings."""

from __future__ import annotations

from typing import Any

from goldset_generator.fact_resolver import resolve_field_display
from goldset_generator.table_schema_builder import build_page_schemas


class QAGenerator:
    """Generate QA benchmarks; expected values resolve from cell references at runtime."""

    def generate_from_table_golds(
        self,
        page_number: int,
        table_golds: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        qa_items: list[dict[str, Any]] = []

        for schema in build_page_schemas(table_golds):
            if schema.get("page_number") != page_number:
                continue

            for row in schema.get("rows") or []:
                subject = row.get("subject")
                if not subject:
                    continue
                fields = row.get("fields") or {}

                cas_field = fields.get("CAS") or {}
                if (
                    cas_field.get("cell_id")
                    and cas_field.get("value_status") != "merged_cell"
                    and resolve_field_display("CAS", cas_field)
                ):
                    qa_items.append(
                        {
                            "question": f"شماره CAS برای {subject} چیست؟",
                            "answer_type": "sql",
                            "intent": "cas_lookup",
                            "target_reference": {
                                "table": "oel_chemical_limits",
                                "field": "CAS",
                                "page_number": page_number,
                                "table_id": schema.get("table_id"),
                            },
                            "expected_cell_ids": [cas_field["cell_id"]],
                            "expected_value": {
                                "source": "cell",
                                "cell_id": cas_field["cell_id"],
                                "field": "CAS",
                            },
                            "query_template": "get_oel_by_chemical",
                            "parameters": {"chemical": subject, "field": "CAS"},
                            "review_status": "pending",
                            "semantic_confidence": 0.92,
                            "_resolve_field": "CAS",
                            "_resolve_field_data": cas_field,
                        }
                    )

                for limit_field, label in (("TWA", "TWA"), ("STEL", "STEL"), ("ceiling", "Ceiling")):
                    field = fields.get(limit_field) or {}
                    if (
                        field.get("cell_id")
                        and field.get("value_status") == "extracted"
                        and resolve_field_display(limit_field, field)
                    ):
                        qa_items.append(
                            {
                                "question": f"حد مجاز {label} برای {subject} چقدر است؟",
                                "answer_type": "sql",
                                "intent": "oel_lookup",
                                "target_reference": {
                                    "table": "oel_chemical_limits",
                                    "field": limit_field,
                                    "page_number": page_number,
                                    "table_id": schema.get("table_id"),
                                },
                                "expected_cell_ids": [field["cell_id"]],
                                "expected_value": {
                                    "source": "cell",
                                    "cell_id": field["cell_id"],
                                    "field": limit_field,
                                },
                                "query_template": "get_oel_by_chemical",
                                "parameters": {"chemical": subject, "field": limit_field},
                                "review_status": "pending",
                                "semantic_confidence": 0.88,
                                "_resolve_field": limit_field,
                                "_resolve_field_data": field,
                            }
                        )

        for formula in formulas:
            if formula.get("status") != "approved":
                continue
            qa_items.append(
                {
                    "question": f"فرمول {formula.get('document_equation_reference') or formula.get('formula_id')} در صفحه {page_number} چگونه است؟",
                    "answer_type": "calculation",
                    "intent": "formula_calculation",
                    "formula_reference": formula.get("formula_id"),
                    "expected_cell_ids": [],
                    "expected_value": {
                        "source": "formula_engine",
                        "formula_id": formula.get("formula_id"),
                        "field": "normalized_expression",
                    },
                    "review_status": "pending",
                    "semantic_confidence": formula.get("confidence", {}).get("semantic", 0.82),
                }
            )

        return qa_items

    def generate(
        self,
        page_number: int,
        page_text: str,
        table_golds: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        *,
        valid_cell_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        page_cell_ids = set(valid_cell_ids or [])
        qa_items = self.generate_from_table_golds(page_number, table_golds, formulas)
        if not page_cell_ids:
            return qa_items
        filtered: list[dict[str, Any]] = []
        for qa in qa_items:
            ids = [cid for cid in (qa.get("expected_cell_ids") or []) if cid in page_cell_ids]
            if qa.get("expected_cell_ids") and not ids:
                continue
            qa["expected_cell_ids"] = ids
            filtered.append(qa)
        return filtered

    def resolve_answers(
        self,
        qa_items: list[dict[str, Any]],
        cell_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach derived answers for candidate review; golden benchmarks use expected_value refs."""
        resolved: list[dict[str, Any]] = []
        for qa in qa_items:
            item = dict(qa)
            field_name = item.pop("_resolve_field", None)
            field_data = item.pop("_resolve_field_data", None)

            if field_name and field_data:
                derived = resolve_field_display(field_name, field_data)
                if derived:
                    item["derived_answer"] = derived
                    item["answer_source"] = "table_gold_field"
            elif item.get("expected_value", {}).get("source") == "formula_engine":
                item["answer_source"] = "formula_engine"
                # derived_answer resolved externally from formula gold when available
            else:
                cell_ids = qa.get("expected_cell_ids") or []
                answer_parts = []
                for cell_id in cell_ids:
                    cell = cell_by_id.get(cell_id)
                    if cell and cell.get("text"):
                        answer_parts.append(cell["text"])
                if answer_parts:
                    item["derived_answer"] = " | ".join(answer_parts)
                    item["answer_source"] = "evidence_cells"

            resolved.append(item)
        return resolved
