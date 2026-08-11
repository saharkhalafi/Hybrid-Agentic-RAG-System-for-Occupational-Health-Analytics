"""Table Family Classifier — deterministic first, Gemini only for ambiguity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from config.logging import get_logger
from database.models import TableType
from knowledge.table_classifier import classify_table_text
from pipeline_contracts.canonical_grid import CanonicalGrid
from schema_registry.registry import get_schema_registry

logger = get_logger(__name__)

SCHEMA_BY_TABLE_TYPE: dict[str, str] = {
    TableType.CHEMICAL_OEL.value: "chemical_oel_v1",
    TableType.VIBRATION.value: "vibration_exposure_v1",
    TableType.NOISE.value: "noise_exposure_v1",
    TableType.BIOLOGICAL_MONITORING.value: "biological_monitoring_v1",
}

AMBIGUOUS_LIMIT_HEADERS = re.compile(
    r"حد\s*مجاز|limit|exposure\s*limit|oel",
    re.IGNORECASE,
)


@dataclass
class TableFamilyClassification:
    schema_id: str | None
    schema_version: str | None
    table_type: str
    confidence: float
    classifier: str
    requires_gemini: bool = False
    rationale: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "table_type": self.table_type,
            "confidence": self.confidence,
            "classifier": self.classifier,
            "requires_gemini": self.requires_gemini,
            "rationale": self.rationale,
        }


class TableFamilyClassifier:
    """Classify table family from CanonicalGrid — outputs schema_id, never cell values."""

    def __init__(self, *, gemini_client: Any | None = None) -> None:
        self.registry = get_schema_registry()
        self.gemini = gemini_client

    def classify_grid(self, grid: CanonicalGrid) -> TableFamilyClassification:
        header_text = " ".join(cell.text for cell in grid.headers)
        body_sample = " ".join(
            cell.text for row in grid.rows[:3] for cell in row.cells
        )
        combined = f"{header_text}\n{body_sample}"

        table_type = classify_table_text(combined).value
        schema_id = SCHEMA_BY_TABLE_TYPE.get(table_type)
        schema = self.registry.get(schema_id) if schema_id else None
        schema_version = str(schema.get("version")) if schema else None

        confidence = _score_confidence(combined, table_type, schema_id)
        ambiguous = _is_ambiguous_header(header_text)

        if ambiguous and confidence < 0.85:
            gemini_result = self._gemini_classify(grid, combined)
            if gemini_result:
                return gemini_result

        return TableFamilyClassification(
            schema_id=schema_id,
            schema_version=schema_version,
            table_type=table_type,
            confidence=confidence,
            classifier="deterministic_rules",
            requires_gemini=ambiguous and confidence < 0.85,
            rationale="rule_based_header_and_content_signals",
        )

    def classify_table_dict(self, table: dict[str, Any]) -> TableFamilyClassification:
        from pipeline_contracts.canonical_grid import CanonicalGridBuilder

        grid = CanonicalGridBuilder(
            pipeline_version="preview",
            processor_version="preview",
        ).from_table_dict(table)
        return self.classify_grid(grid)

    def _gemini_classify(
        self, grid: CanonicalGrid, combined_text: str
    ) -> TableFamilyClassification | None:
        if self.gemini is None or not getattr(self.gemini, "available", lambda: False)():
            return None

        headers = [cell.text for cell in grid.headers]
        prompt = (
            "Classify this table into ONE schema_id from the registry. "
            "Output JSON only: {\"schema_id\": \"...\", \"confidence\": 0.0-1.0, \"rationale\": \"...\"}. "
            "Do NOT output any cell values or numbers.\n\n"
            f"Headers: {headers}\n"
            f"Sample text: {combined_text[:2000]}"
        )
        result = self.gemini.generate_json(prompt)
        if not isinstance(result, dict):
            return None

        schema_id = result.get("schema_id")
        schema = self.registry.get(schema_id) if schema_id else None
        if not schema:
            return None

        logger.info("table_family_gemini_fallback", table_id=grid.table_id, schema_id=schema_id)
        return TableFamilyClassification(
            schema_id=schema_id,
            schema_version=str(schema.get("version")),
            table_type=str(schema.get("table_type") or "unknown"),
            confidence=float(result.get("confidence") or 0.75),
            classifier="gemini_fallback",
            requires_gemini=True,
            rationale=str(result.get("rationale") or "gemini_ambiguity_resolution"),
        )


def _score_confidence(text: str, table_type: str, schema_id: str | None) -> float:
    if not schema_id or table_type == TableType.UNKNOWN.value:
        return 0.35
    if table_type == TableType.CHEMICAL_OEL.value:
        cas_hits = len(re.findall(r"\[\d{2,7}-\d{2}-\d\]", text))
        limit_hits = len(re.findall(r"\b(TWA|STEL|ppm|mg/m)\b", text, re.I))
        if cas_hits >= 2 and limit_hits >= 2:
            return 0.98
        if cas_hits >= 1:
            return 0.85
        return 0.65
    return 0.75


def _is_ambiguous_header(header_text: str) -> bool:
    if not header_text.strip():
        return True
    if AMBIGUOUS_LIMIT_HEADERS.search(header_text):
        lower = header_text.lower()
        twa = "twa" in lower
        stel = "stel" in lower
        ceiling = "ceiling" in lower or bool(re.search(r"\bc\b", lower))
        defined = sum([twa, stel, ceiling])
        return defined == 0
    return False
