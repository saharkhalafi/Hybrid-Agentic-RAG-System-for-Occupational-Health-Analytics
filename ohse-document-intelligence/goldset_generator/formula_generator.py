"""Evidence-first formula gold generation pipeline."""

from __future__ import annotations

from typing import Any

from config.logging import get_logger
from extraction.formula_candidate_detector import detect_formula_candidates
from extraction.formula_reconstructor import reconstruct_formula
from goldset_generator.formula_semantic_mapper import FormulaSemanticMapper
from goldset_generator.formula_validator import FormulaValidator
from goldset_generator.gemini_client import GeminiClient

logger = get_logger(__name__)


def _normalize_semantics(semantics: dict[str, Any]) -> dict[str, Any]:
    """Normalize Gemini/deterministic semantics into a consistent gold shape."""
    normalized = dict(semantics)
    variables = normalized.get("variables")
    if isinstance(variables, list):
        normalized["variables"] = {
            (item.get("symbol") or item.get("name") or f"var_{idx}"): {
                "role": item.get("role", "unknown"),
                "description": item.get("description", ""),
            }
            for idx, item in enumerate(variables)
            if isinstance(item, dict)
        }
    units = normalized.get("units")
    if isinstance(units, list):
        unit_map: dict[str, str] = {}
        for item in units:
            if isinstance(item, dict) and item.get("variable") and item.get("unit"):
                var = str(item["variable"])
                unit_map.setdefault(var, str(item["unit"]))
        normalized["units"] = unit_map
    elif isinstance(units, dict):
        flat_units: dict[str, str] = {}
        for key, value in units.items():
            if isinstance(value, list):
                flat_units[str(key)] = str(value[0]) if value else ""
            else:
                flat_units[str(key)] = str(value)
        normalized["units"] = flat_units
    confidence = normalized.get("semantic_confidence")
    if isinstance(confidence, str):
        normalized["semantic_confidence"] = {"high": 0.85, "medium": 0.65, "low": 0.4}.get(
            confidence.lower(), 0.5
        )
    return normalized


def _sanitize_reference_values(
    semantics: dict[str, Any],
    page_text: str,
    fallback: dict[str, Any],
) -> list[dict[str, Any]]:
    refs = list(semantics.get("reference_values") or [])
    cleaned: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        value = str(ref.get("reference_value") or "").strip()
        if not value or value.lower() in {"field_name", "unknown", "null", "none"}:
            continue
        if value in page_text or value in page_text.replace(" ", ""):
            cleaned.append(ref)
    if cleaned:
        return cleaned
    return list(fallback.get("reference_values") or [])


class FormulaGoldGenerator:
    def __init__(self, gemini_client: GeminiClient | None = None) -> None:
        self.semantic_mapper = FormulaSemanticMapper(gemini_client)
        self.validator = FormulaValidator()

    def generate_for_page(
        self,
        page_number: int,
        page_text: str,
        paragraphs: list[dict[str, Any]],
        cells: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Returns:
          - approved formulas (for QA / downstream)
          - review items (uncertain formulas)
          - all formula records (for writing gold/review artifacts)
        """
        del cells  # reserved for future cell-linked formula evidence
        candidates = detect_formula_candidates(page_number, page_text, paragraphs)
        if not candidates:
            return [], [], []

        approved: list[dict[str, Any]] = []
        all_records: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []

        for index, candidate in enumerate(candidates, 1):
            reconstruction = reconstruct_formula(candidate)
            semantics_raw = self.semantic_mapper.interpret(candidate, reconstruction, page_text) or {}
            semantics = _normalize_semantics(semantics_raw)
            if not semantics.get("units") and candidate.document_equation_reference in {"2", "3"}:
                fallback = self.semantic_mapper._deterministic_fallback(
                    candidate, reconstruction, page_text
                )
                semantics.setdefault("units", fallback.get("units") or {})
            fallback = self.semantic_mapper._deterministic_fallback(
                candidate, reconstruction, page_text
            )
            semantics["reference_values"] = _sanitize_reference_values(
                semantics, page_text, fallback
            )
            formula_id = f"formula_{page_number:03d}_{index:02d}"
            raw_fragments = [
                fragment.get("text", "")
                for fragment in candidate.evidence_text_fragments
                if fragment.get("text")
            ]

            gold: dict[str, Any] = {
                "formula_id": formula_id,
                "document_equation_reference": candidate.document_equation_reference,
                "evidence": {
                    "page": page_number,
                    "bbox": candidate.bbox,
                    "candidate_text": candidate.candidate_text,
                    "text_before": candidate.text_before,
                    "text_after": candidate.text_after,
                    "raw_text_fragments": raw_fragments,
                    "fragments": candidate.evidence_text_fragments,
                    "nearby_math_tokens": candidate.nearby_math_tokens,
                    "source": candidate.source,
                },
                "reconstruction": reconstruction.to_dict(),
                "semantics": {
                    "formula_type": semantics.get("formula_type"),
                    "variables": semantics.get("variables") or {},
                    "units": semantics.get("units") or {},
                    "reference_values": semantics.get("reference_values") or [],
                },
                "raw_expression": reconstruction.raw_expression,
                "normalized_expression": reconstruction.expression,
                "confidence": {
                    "structure": 0.85 if reconstruction.status == "validated" else 0.35,
                    "semantic": semantics.get("semantic_confidence", 0.5),
                    "overall": 0.0,
                },
                "source_reference": {
                    "page": page_number,
                    "bbox": candidate.bbox,
                    "evidence_text_ids": candidate.paragraph_indices,
                    "extraction_method": "layout_reconstruction",
                },
            }

            validation = self.validator.validate(
                gold,
                page_text,
                reconstruction_status=reconstruction.status,
            )
            gold["validation"] = validation
            gold["status"] = "approved" if validation["overall"] == "approved" else "extraction_uncertain"
            semantic_conf = semantics.get("semantic_confidence", 0.5)
            if not isinstance(semantic_conf, (int, float)):
                semantic_conf = 0.5
            gold["confidence"]["overall"] = min(
                gold["confidence"]["structure"],
                float(semantic_conf),
            )

            all_records.append(gold)
            if gold["status"] == "approved":
                approved.append(gold)
            else:
                review_items.append(
                    {
                        "page_number": page_number,
                        "item_type": "formula",
                        "formula_id": formula_id,
                        "issues": validation.get("issues") or [],
                        "status": gold["status"],
                        "source_reference": gold["source_reference"],
                        "candidate": candidate.to_dict(),
                        "reconstruction": reconstruction.to_dict(),
                        "formula_record": gold,
                    }
                )
                logger.info(
                    "formula_extraction_uncertain",
                    page=page_number,
                    formula_id=formula_id,
                    expression=reconstruction.expression,
                )

        return approved, review_items, all_records
