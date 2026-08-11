"""Validate goldset artifacts — reject hallucinated numerical values."""

from __future__ import annotations

import re
from typing import Any

from goldset_generator.fact_resolver import cell_matches_predicate, cell_page_number

from pipeline_contracts.numeric_integrity import (
    numeric_value_mismatch,
    try_normalize,
    validate_row_number_cell,
)

NUMERIC_PATTERN = re.compile(
    r"\b\d+(?:[./]\d+)?\s*(?:ppm|mg/m³|mg/m3|dBA|mg/L|f/ml|%)\b"
    r"|\b\d{2,7}-\d{2}-\d\b"
    r"|\b\d+(?:[./]\d+)?\b",
    re.IGNORECASE,
)
NUMERIC_TOKEN_PATTERN = re.compile(r"\d+(?:[./]\d+)?|\d{2,7}-\d{2}-\d")
CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")
SCHEMA_LABELS = frozenset(
    {"cas number", "twa", "stel", "ceiling", "exposure limit", "molecular weight"}
)
DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


class GoldsetValidator:
    """Ensure semantic layer never introduces numbers not in evidence layer."""

    def __init__(self, evidence_cells: list[dict[str, Any]]) -> None:
        self.evidence_cells = evidence_cells
        self.cell_by_id = {cell["cell_id"]: cell for cell in evidence_cells if cell.get("cell_id")}

    @staticmethod
    def _normalize(value: str) -> str:
        # Evidence commonly contains Persian/Arabic-Indic digits while extracted
        # values use ASCII. Translate before traceability comparisons so
        # ۲۶۷/۳۷ and 267.37 are treated as equivalent representations.
        translated = value.translate(DIGIT_TRANSLATION)
        return re.sub(r"\s+", " ", translated.strip().lower())

    def numeric_exists_in_cell(self, cell_id: str | None, value: str) -> bool:
        if not cell_id or not value:
            return False
        cell = self.cell_by_id.get(cell_id)
        if not cell:
            return False
        cell_text = self._normalize(cell.get("text") or "")
        cell_norm = self._normalize(cell.get("normalized_value") or "")
        token = self._normalize(str(value))
        if token in cell_text or token in cell_norm:
            return True
        token_compact = re.sub(r"\s+", "", token)
        cell_text_compact = re.sub(r"\s+", "", cell_text)
        cell_norm_compact = re.sub(r"\s+", "", cell_norm)
        if token_compact in cell_text_compact or token_compact in cell_norm_compact:
            return True
        for match in NUMERIC_TOKEN_PATTERN.findall(str(value)):
            if self._normalize(match) in cell_text or self._normalize(match) in cell_norm:
                return True
            match_compact = re.sub(r"\s+", "", self._normalize(match))
            if match_compact in cell_text_compact or match_compact in cell_norm_compact:
                return True
            if match in (cell.get("text") or "") or match in (cell.get("normalized_value") or ""):
                return True
        canonical_value = self._normalize(str(value)).replace(" ", "")
        # Accept normalized molecular weights that appear with an OCR slash
        # decimal (267/37 -> 267.37), including Persian/Arabic-Indic digits.
        if re.fullmatch(r"\d+\.\d+", canonical_value):
            parts = canonical_value.split(".")
            if len(parts) == 2 and f"{parts[1]}/{parts[0]}" in cell_text.replace(" ", ""):
                return True
            if f"{parts[0]}/{parts[1]}" in cell_text.replace(" ", ""):
                return True
        # Accept slash-decimal OCR forms (0009/0 or 0/0009 -> 0.0009).
        if re.fullmatch(r"0\.\d+", canonical_value):
            decimal_part = canonical_value.split(".", 1)[1]
            compact = re.sub(r"\s+", "", cell_text)
            compact_norm = re.sub(r"\s+", "", cell_norm)
            for form in (f"{decimal_part}/0", f"0/{decimal_part}"):
                if form in compact or form in compact_norm:
                    return True
            for source in (cell.get("text") or "", cell.get("normalized_value") or ""):
                if re.search(rf"{re.escape(decimal_part)}\s*/\s*0\b", source):
                    return True
                if re.search(rf"0\s*/\s*{re.escape(decimal_part)}\b", source):
                    return True
        return False

    def _validate_provenance(
        self,
        source_reference: dict[str, Any],
        *,
        require_page: bool = False,
        page_number: int | None = None,
    ) -> list[str]:
        issues: list[str] = []
        cell_ids = list(source_reference.get("cell_ids") or [])
        if source_reference.get("cell_id"):
            cell_ids.append(source_reference["cell_id"])
        cell_ids = list(dict.fromkeys(cell_ids))
        if not cell_ids:
            issues.append("missing cell-level provenance (cell_ids)")
            return issues
        for cell_id in cell_ids:
            if cell_id not in self.cell_by_id:
                issues.append(f"references unknown cell_id: {cell_id}")
            elif page_number is not None:
                cell_page = cell_page_number(cell_id)
                if cell_page is not None and cell_page != page_number:
                    issues.append(f"cell_id {cell_id} belongs to page {cell_page}, expected {page_number}")
        if require_page and not source_reference.get("page_number"):
            issues.append("missing page_number in source_reference")
        return issues

    def validate_entity(self, entity: dict[str, Any], *, page_number: int | None = None) -> list[str]:
        issues: list[str] = []
        if "value" in entity:
            issues.append("entity contains forbidden 'value' field")

        entity_type = entity.get("type", "")
        text = str(entity.get("text") or entity.get("resolved_value") or "").strip()
        normalized = self._normalize(text)

        if text and normalized in SCHEMA_LABELS:
            issues.append(f"entity text is schema label, not evidence value: {text!r}")

        linked = entity.get("linked_cell_ids") or []
        if entity.get("linked_cell_id"):
            linked = [entity["linked_cell_id"], *linked]
        if not linked:
            issues.append("entity missing linked_cell_ids")

        ref = dict(entity.get("source_reference") or {})
        ref["cell_ids"] = linked
        issues.extend(self._validate_provenance(ref, page_number=page_number))

        if entity_type == "cas_number":
            cas_text = str(entity.get("resolved_value") or text)
            if cas_text and not re.search(r"\d{2,7}-\d{2}-\d", cas_text):
                issues.append(f"cas_number entity must contain CAS digits, got {cas_text!r}")

        if entity_type == "exposure_limit":
            limit_text = str(entity.get("resolved_value") or text)
            if limit_text and not re.search(r"(ppm|mg/m³|mg/m3)", limit_text, re.I):
                if not NUMERIC_PATTERN.search(limit_text):
                    issues.append(f"exposure_limit entity must contain limit value, got {limit_text!r}")

        check_value = str(entity.get("resolved_value") or text)
        if check_value and entity_type in {"cas_number", "exposure_limit", "molecular_weight"}:
            for cell_id in linked:
                if not self.numeric_exists_in_cell(cell_id, check_value):
                    issues.append(f"entity value not in linked cell: {check_value!r}")
        elif check_value and entity_type not in {"chemical"} and NUMERIC_PATTERN.search(check_value):
            for cell_id in linked:
                if not self.numeric_exists_in_cell(cell_id, check_value):
                    issues.append(f"entity text numeric not in linked cell: {check_value!r}")
        return issues

    def validate_triple(self, triple: dict[str, Any], *, page_number: int | None = None) -> list[str]:
        issues: list[str] = []
        ref = triple.get("source_reference") or triple.get("object_reference") or {}
        if not isinstance(ref, dict):
            ref = {}
        cell_id = ref.get("cell_id")
        cell_ids = ref.get("cell_ids") or ([cell_id] if cell_id else [])
        if not cell_ids:
            issues.append("triple has no resolvable object reference")
        else:
            for cid in cell_ids:
                if cid not in self.cell_by_id:
                    issues.append(f"triple references unknown cell_id: {cid}")
                elif page_number is not None:
                    cell_page = cell_page_number(cid)
                    if cell_page is not None and cell_page != page_number:
                        issues.append(
                            f"triple cell_id {cid} belongs to page {cell_page}, expected {page_number}"
                        )

        obj = triple.get("object")
        predicate = triple.get("predicate", "")
        if obj and isinstance(obj, str):
            resolved_cell = cell_ids[0] if cell_ids else None
            if predicate and not cell_matches_predicate(predicate, obj, resolved_value=obj):
                issues.append(f"triple predicate {predicate} incompatible with object {obj!r}")

            if NUMERIC_PATTERN.search(obj):
                if not self.numeric_exists_in_cell(resolved_cell, obj):
                    if predicate != "has_CAS" or not re.search(r"\d{2,7}-\d{2}-\d", obj):
                        issues.append(f"triple object numeric not in source cell: {obj!r}")
        return issues

    def validate_qa(self, qa: dict[str, Any], *, page_number: int | None = None) -> list[str]:
        issues: list[str] = []
        for cell_id in qa.get("expected_cell_ids") or []:
            if cell_id not in self.cell_by_id:
                issues.append(f"QA references unknown cell_id: {cell_id}")
            elif page_number is not None:
                cell_page = cell_page_number(cell_id)
                if cell_page is not None and cell_page != page_number:
                    issues.append(f"QA cell_id {cell_id} belongs to page {cell_page}, expected {page_number}")

        answer_source = qa.get("answer_source")
        derived = qa.get("derived_answer") or qa.get("answer")
        expected = qa.get("expected_value") or {}

        if not expected and not qa.get("expected_cell_ids"):
            issues.append("QA missing expected_value or expected_cell_ids")

        if derived and answer_source not in {"table_gold_field", "evidence_cells", "formula_engine"}:
            issues.append("QA derived_answer must specify answer_source")

        if derived and answer_source == "evidence_cells":
            for cell_id in qa.get("expected_cell_ids") or []:
                if not self.numeric_exists_in_cell(cell_id, str(derived)):
                    if NUMERIC_PATTERN.search(str(derived)):
                        issues.append(f"QA derived_answer numeric not in expected source cell: {derived!r}")

        if derived and answer_source == "table_gold_field":
            target = qa.get("target_reference") or {}
            field = target.get("field") or expected.get("field")
            if field == "CAS" and not re.search(r"\d{2,7}-\d{2}-\d", str(derived)):
                issues.append(f"QA CAS derived_answer invalid: {derived!r}")
        return issues

    def validate_table_field(self, field_name: str, field_data: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        if not isinstance(field_data, dict):
            return issues
        if field_data.get("value_status") == "merged_cell":
            return issues
        value = field_data.get("value")
        cell_id = field_data.get("cell_id")
        if value is None:
            return issues

        cell = self.cell_by_id.get(cell_id) if cell_id else None
        source_text = (cell or {}).get("text") or ""

        if field_name == "row_number":
            orig = field_data.get("original_value") or str(value)
            for msg in validate_row_number_cell(str(orig)):
                issues.append(f"CRITICAL: {msg}")
            if source_text and str(value) != source_text.strip():
                if not self.numeric_exists_in_cell(cell_id, str(value)):
                    issues.append(
                        f"CRITICAL NUMERIC_VALUE_MISMATCH: row_number value {value!r} "
                        f"differs from source {source_text!r}"
                    )

        if field_name == "CAS" and cell_id:
            if value and not re.search(rf"\[{re.escape(str(value))}\]", source_text):
                if str(value) not in source_text:
                    issues.append(
                        f"CRITICAL: table field CAS value {value!r} not in source cell {cell_id}"
                    )
            if not field_data.get("bbox") and field_data.get("value_status") == "extracted":
                issues.append(f"table field CAS missing bbox")
            return issues

        if field_name in {"TWA", "STEL", "ceiling", "molecular_weight", "row_number"} and cell_id:
            normalized = field_data.get("normalized_value")

            if normalized and str(value) != str(normalized):
                provable = not numeric_value_mismatch(str(value), str(normalized), source_text)
                if not provable:
                    norm_from_token, _ = try_normalize(
                        str(value),
                        field_type=field_name.lower() if field_name != "molecular_weight" else "molecular_weight",
                    )
                    if norm_from_token == str(normalized) and self.numeric_exists_in_cell(cell_id, str(value)):
                        provable = True
                if not provable:
                    issues.append(
                        f"CRITICAL NUMERIC_VALUE_MISMATCH: {field_name} normalized {normalized!r} "
                        f"not provable from source {source_text!r}"
                    )

            if field_name == "ceiling" and str(value).strip().upper().startswith("C"):
                if not re.search(r"\bC\b", source_text, re.IGNORECASE):
                    issues.append(
                        f"CRITICAL NUMERIC_VALUE_MISMATCH: {field_name} value {value!r} "
                        f"not in source cell {cell_id}"
                    )
            elif not self.numeric_exists_in_cell(cell_id, str(value)):
                if NUMERIC_PATTERN.search(str(value)):
                    issues.append(
                        f"CRITICAL: table field {field_name} value {value!r} not in source cell {cell_id}"
                    )
        if not field_data.get("bbox") and field_data.get("value_status") == "extracted":
            issues.append(f"table field {field_name} missing bbox")
        return issues

    def compute_final_confidence(
        self,
        *,
        ocr_confidence: float | None,
        structural_confidence: float | None,
        semantic_confidence: float | None,
        bbox_confidence: float | None = None,
    ) -> float | str:
        scores = [
            score
            for score in (ocr_confidence, structural_confidence, semantic_confidence, bbox_confidence)
            if score is not None
        ]
        if not scores:
            return "unknown"
        return min(scores)

    def review_priority(self, confidence: float | str) -> str:
        if confidence == "unknown":
            return "high_priority_review"
        if confidence >= 0.85:
            return "auto_accepted_candidate"
        if confidence >= 0.65:
            return "review_required"
        return "high_priority_review"
