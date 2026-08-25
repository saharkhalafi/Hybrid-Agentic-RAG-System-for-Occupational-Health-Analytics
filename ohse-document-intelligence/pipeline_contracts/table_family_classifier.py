"""Table Family Classifier — deterministic first, Gemini only for ambiguity.

The classifier is intentionally deterministic for known OHSE table families.
Gemini is used only when deterministic signals are insufficient.
"""

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


# ---------------------------------------------------------------------------
# Schema mapping
# ---------------------------------------------------------------------------

SCHEMA_BY_TABLE_TYPE: dict[str, str] = {
    TableType.CHEMICAL_OEL.value: "chemical_oel_v1",
    TableType.VIBRATION.value: "vibration_exposure_v1",
    TableType.NOISE.value: "noise_exposure_v1",
    TableType.BIOLOGICAL_MONITORING.value: "biological_monitoring_v1",
}


# ---------------------------------------------------------------------------
# Regex signatures
# ---------------------------------------------------------------------------

# CAS registry number:
#
# 67-56-1
# 75-05-8
# [75-05-8]
# CAS 67-56-1
#
CAS_PATTERN = re.compile(
    r"\b(?:CAS\s*)?(?:\[?\d{2,7}-\d{2}-\d\]?)\b",
    re.IGNORECASE,
)


TWA_PATTERN = re.compile(
    r"\bTWA\b",
    re.IGNORECASE,
)

STEL_PATTERN = re.compile(
    r"\bSTEL(?:/C)?\b",
    re.IGNORECASE,
)

OEL_PATTERN = re.compile(
    r"\bOEL\b",
    re.IGNORECASE,
)

CHEMICAL_UNIT_PATTERN = re.compile(
    r"\b(?:ppm|ppb|mg/m(?:3|³)|µg/m(?:3|³)|ug/m(?:3|3)|mg\/m2)\b",
    re.IGNORECASE,
)


AMBIGUOUS_LIMIT_HEADERS = re.compile(
    r"حد\s*مجاز|limit|exposure\s*limit|oel",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

class TableFamilyClassifier:
    """Classify table family from CanonicalGrid.

    Important:
    - Classification never modifies cell values.
    - Deterministic rules run first.
    - Gemini is only a fallback for genuinely ambiguous tables.
    """

    def __init__(self, *, gemini_client: Any | None = None) -> None:
        self.registry = get_schema_registry()
        self.gemini = gemini_client

    # ------------------------------------------------------------------
    # Canonical grid
    # ------------------------------------------------------------------

    def classify_grid(
        self,
        grid: CanonicalGrid,
    ) -> TableFamilyClassification:

        header_text = " ".join(
            cell.text
            for cell in grid.headers
            if cell.text
        )

        body_sample = " ".join(
            cell.text
            for row in grid.rows[:3]
            for cell in row.cells
            if cell.text
        )

        combined = f"{header_text}\n{body_sample}".strip()

        # --------------------------------------------------------------
        # First: existing domain classifier
        # --------------------------------------------------------------

        table_type = classify_table_text(combined).value

        # --------------------------------------------------------------
        # Second: structural deterministic fallback
        #
        # The generic classifier intentionally requires strong signatures.
        # For CanonicalGrid we have additional structural evidence such as:
        #
        #   TWA
        #   CAS
        #   ppm / mg/m3
        #
        # which is enough to identify a chemical OEL table.
        # --------------------------------------------------------------

        if table_type == TableType.UNKNOWN.value:

            fallback_type = _classify_structural_fallback(
                header_text=header_text,
                body_text=body_sample,
                combined_text=combined,
            )

            if fallback_type is not None:
                table_type = fallback_type

        # --------------------------------------------------------------
        # Resolve schema
        # --------------------------------------------------------------

        schema_id = SCHEMA_BY_TABLE_TYPE.get(table_type)

        schema = (
            self.registry.get(schema_id)
            if schema_id
            else None
        )

        schema_version = (
            str(schema.get("version"))
            if schema
            else None
        )

        # --------------------------------------------------------------
        # Confidence
        # --------------------------------------------------------------

        confidence = _score_confidence(
            combined,
            table_type,
            schema_id,
        )

        # --------------------------------------------------------------
        # Gemini ambiguity
        # --------------------------------------------------------------

        ambiguous = _is_ambiguous_header(header_text)

        if ambiguous and confidence < 0.85:

            gemini_result = self._gemini_classify(
                grid,
                combined,
            )

            if gemini_result:
                return gemini_result

        return TableFamilyClassification(
            schema_id=schema_id,
            schema_version=schema_version,
            table_type=table_type,
            confidence=confidence,
            classifier="deterministic_rules",
            requires_gemini=(
                ambiguous
                and confidence < 0.85
            ),
            rationale="rule_based_header_and_content_signals",
        )

    # ------------------------------------------------------------------
    # Dictionary input
    # ------------------------------------------------------------------

    def classify_table_dict(
        self,
        table: dict[str, Any],
    ) -> TableFamilyClassification:

        from pipeline_contracts.canonical_grid import (
            CanonicalGridBuilder,
        )

        grid = CanonicalGridBuilder(
            pipeline_version="preview",
            processor_version="preview",
        ).from_table_dict(table)

        return self.classify_grid(grid)

    # ------------------------------------------------------------------
    # Gemini fallback
    # ------------------------------------------------------------------

    def _gemini_classify(
        self,
        grid: CanonicalGrid,
        combined_text: str,
    ) -> TableFamilyClassification | None:

        if (
            self.gemini is None
            or not getattr(
                self.gemini,
                "available",
                lambda: False,
            )()
        ):
            return None

        headers = [
            cell.text
            for cell in grid.headers
        ]

        prompt = (
            "Classify this table into ONE schema_id "
            "from the registry.\n"
            "Output JSON only:\n"
            '{"schema_id": "...", '
            '"confidence": 0.0-1.0, '
            '"rationale": "..."}\n\n'
            "Do NOT output any cell values or numbers.\n\n"
            f"Headers: {headers}\n"
            f"Sample text: {combined_text[:2000]}"
        )

        result = self.gemini.generate_json(prompt)

        if not isinstance(result, dict):
            return None

        schema_id = result.get("schema_id")

        schema = (
            self.registry.get(schema_id)
            if schema_id
            else None
        )

        if not schema:
            return None

        logger.info(
            "table_family_gemini_fallback",
            table_id=grid.table_id,
            schema_id=schema_id,
        )

        return TableFamilyClassification(
            schema_id=schema_id,
            schema_version=str(schema.get("version")),
            table_type=str(
                schema.get("table_type")
                or "unknown"
            ),
            confidence=float(
                result.get("confidence")
                or 0.75
            ),
            classifier="gemini_fallback",
            requires_gemini=True,
            rationale=str(
                result.get("rationale")
                or "gemini_ambiguity_resolution"
            ),
        )


# ---------------------------------------------------------------------------
# Structural fallback classifier
# ---------------------------------------------------------------------------

def _classify_structural_fallback(
    *,
    header_text: str,
    body_text: str,
    combined_text: str,
) -> str | None:
    """Identify known table families from strong structural signals.

    This is deliberately conservative.

    Chemical OEL:
        - CAS number
        - TWA / STEL / OEL
        - exposure unit such as ppm or mg/m3

    Noise:
        - LAeq / LEX / dBA / dose / criterion

    Vibration:
        - A(8) / VDV / m/s2 / hand-arm / whole-body
    """

    text = combined_text.lower()

    # --------------------------------------------------------------
    # Chemical OEL
    # --------------------------------------------------------------

    cas_hits = len(
        CAS_PATTERN.findall(combined_text)
    )

    twa_hit = bool(
        TWA_PATTERN.search(combined_text)
    )

    stel_hit = bool(
        STEL_PATTERN.search(combined_text)
    )

    oel_hit = bool(
        OEL_PATTERN.search(combined_text)
    )

    chemical_unit_hit = bool(
        CHEMICAL_UNIT_PATTERN.search(combined_text)
    )

    # Strongest signature:
    #
    # CAS + TWA + chemical exposure unit
    #
    if (
        cas_hits >= 1
        and twa_hit
        and chemical_unit_hit
    ):
        return TableType.CHEMICAL_OEL.value

    # CAS + STEL + exposure unit
    if (
        cas_hits >= 1
        and stel_hit
        and chemical_unit_hit
    ):
        return TableType.CHEMICAL_OEL.value

    # CAS + TWA + STEL
    if (
        cas_hits >= 1
        and twa_hit
        and stel_hit
    ):
        return TableType.CHEMICAL_OEL.value

    # CAS + OEL + exposure unit
    if (
        cas_hits >= 1
        and oel_hit
        and chemical_unit_hit
    ):
        return TableType.CHEMICAL_OEL.value

    # --------------------------------------------------------------
    # Noise
    # --------------------------------------------------------------

    noise_signals = 0

    if re.search(r"\blaeq\b", text):
        noise_signals += 1

    if re.search(r"\bleq\b", text):
        noise_signals += 1

    if re.search(r"\bdba\b", text):
        noise_signals += 1

    if re.search(r"\bdose\b", text):
        noise_signals += 1

    if re.search(r"criterion\s+level", text):
        noise_signals += 1

    if noise_signals >= 2:
        return TableType.NOISE.value

    # --------------------------------------------------------------
    # Vibration
    # --------------------------------------------------------------

    vibration_signals = 0

    if re.search(r"\ba\(8\)", text):
        vibration_signals += 1

    if re.search(r"\bvdv\b", text):
        vibration_signals += 1

    if re.search(r"m/s\s*(?:2|²)", text):
        vibration_signals += 1

    if "hand-arm vibration" in text:
        vibration_signals += 1

    if "whole-body vibration" in text:
        vibration_signals += 1

    if vibration_signals >= 2:
        return TableType.VIBRATION.value

    return None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def _score_confidence(
    text: str,
    table_type: str,
    schema_id: str | None,
) -> float:

    if (
        not schema_id
        or table_type == TableType.UNKNOWN.value
    ):
        return 0.35

    if table_type == TableType.CHEMICAL_OEL.value:

        cas_hits = len(
            CAS_PATTERN.findall(text)
        )

        twa_hits = len(
            TWA_PATTERN.findall(text)
        )

        stel_hits = len(
            STEL_PATTERN.findall(text)
        )

        unit_hits = len(
            CHEMICAL_UNIT_PATTERN.findall(text)
        )

        # Very strong chemical OEL signature
        if (
            cas_hits >= 1
            and twa_hits >= 1
            and stel_hits >= 1
            and unit_hits >= 1
        ):
            return 0.98

        # CAS + TWA + unit
        if (
            cas_hits >= 1
            and twa_hits >= 1
            and unit_hits >= 1
        ):
            return 0.95

        # CAS + STEL + unit
        if (
            cas_hits >= 1
            and stel_hits >= 1
            and unit_hits >= 1
        ):
            return 0.95

        # CAS + TWA
        if (
            cas_hits >= 1
            and twa_hits >= 1
        ):
            return 0.90

        # CAS alone
        if cas_hits >= 1:
            return 0.80

        return 0.65

    if table_type == TableType.NOISE.value:
        return 0.90

    if table_type == TableType.VIBRATION.value:
        return 0.90

    return 0.75


# ---------------------------------------------------------------------------
# Header ambiguity
# ---------------------------------------------------------------------------

def _is_ambiguous_header(
    header_text: str,
) -> bool:

    if not header_text.strip():
        return True

    if AMBIGUOUS_LIMIT_HEADERS.search(
        header_text
    ):
        lower = header_text.lower()

        twa = "twa" in lower
        stel = "stel" in lower

        ceiling = (
            "ceiling" in lower
            or bool(
                re.search(
                    r"\bc\b",
                    lower,
                )
            )
        )

        defined = sum(
            [
                twa,
                stel,
                ceiling,
            ]
        )

        return defined == 0

    return False