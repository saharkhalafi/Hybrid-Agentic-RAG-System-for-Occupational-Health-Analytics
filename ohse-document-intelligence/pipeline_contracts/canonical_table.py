"""Canonical Table — schema-mapped artifact consumed by validation, QA, RAG, and domain DB."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import PROJECT_ROOT
from pipeline_contracts.confidence import LayeredConfidence
from pipeline_contracts.provenance import PipelineStage, ProcessorProvenance
from pipeline_contracts.table_family_classifier import TableFamilyClassification
from pipeline_contracts.validation_codes import ValidationIssue

logger = get_logger(__name__)

CANONICAL_TABLE_ROOT = PROJECT_ROOT / "data" / "canonical" / "tables"


@dataclass
class CanonicalTableRow:
    row_index: int
    fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"row_index": self.row_index, "fields": self.fields}


@dataclass
class CanonicalTable:
    table_id: str
    page_number: int
    schema_id: str
    schema_version: str
    headers: list[str]
    rows: list[CanonicalTableRow]
    classification: TableFamilyClassification
    confidence: LayeredConfidence = field(default_factory=LayeredConfidence)
    provenance: ProcessorProvenance | None = None
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    canonical_grid_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "page_number": self.page_number,
            "schema": self.schema_id,
            "schema_version": self.schema_version,
            "headers": self.headers,
            "rows": [row.to_dict() for row in self.rows],
            "classification": self.classification.to_dict(),
            "confidence": self.confidence.to_dict(),
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "validation": [issue.to_dict() for issue in self.validation_issues],
            "canonical_grid_ref": self.canonical_grid_ref,
            "evidence_refs": self.evidence_refs,
        }


class CanonicalTableBuilder:
    """Convert table gold + classification into the parser-agnostic Canonical Table JSON."""

    @staticmethod
    def from_table_gold(
        table_gold: dict[str, Any],
        classification: TableFamilyClassification,
        *,
        pipeline_version: str,
        processor_version: str,
        validation_issues: list[ValidationIssue] | None = None,
        canonical_grid_ref: str | None = None,
        evidence_refs: list[str] | None = None,
        mapping_confidence: float | None = None,
    ) -> CanonicalTable:
        schema_id = classification.schema_id or "unknown"
        schema_version = str(classification.schema_version or "0")

        header_fields: list[str] = []
        seen: set[str] = set()
        for row in table_gold.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for field_name in row:
                if field_name not in seen:
                    seen.add(field_name)
                    header_fields.append(field_name)

        canonical_rows: list[CanonicalTableRow] = []
        for idx, row in enumerate(table_gold.get("rows") or []):
            if not isinstance(row, dict):
                continue
            fields: dict[str, Any] = {}
            for field_name, field_val in row.items():
                if not isinstance(field_val, dict):
                    continue
                fields[field_name] = {
                    "value": field_val.get("value"),
                    "unit": field_val.get("unit"),
                    "value_status": field_val.get("value_status"),
                    "cell_id": field_val.get("cell_id"),
                    "bbox": field_val.get("bbox"),
                    "provenance": {
                        **(field_val.get("source_reference") or {}),
                        "original_value": field_val.get("original_value"),
                    },
                }
            canonical_rows.append(CanonicalTableRow(row_index=idx, fields=fields))

        structural_conf = _float_or_none(table_gold.get("structural_confidence"))
        val_conf = 1.0 if not validation_issues else 0.7

        return CanonicalTable(
            table_id=table_gold["table_id"],
            page_number=int(table_gold.get("page_number") or 0),
            schema_id=schema_id,
            schema_version=schema_version,
            headers=header_fields,
            rows=canonical_rows,
            classification=classification,
            confidence=LayeredConfidence(
                layout=structural_conf,
                geometry=structural_conf,
                mapping=mapping_confidence or classification.confidence,
                validation=val_conf,
            ),
            provenance=ProcessorProvenance(
                processor="deterministic_semantic_mapper",
                processor_version=processor_version,
                pipeline_version=pipeline_version,
                schema_id=schema_id,
                schema_version=schema_version,
                pipeline_stage=PipelineStage.SEMANTIC.value,
                confidence_source="table_gold_generator",
            ),
            validation_issues=validation_issues or [],
            canonical_grid_ref=canonical_grid_ref,
            evidence_refs=evidence_refs or [],
        )

    @staticmethod
    def write_all(
        tables: list[CanonicalTable],
        *,
        content_hash: str,
        start_page: int,
        end_page: int,
        root: Path | None = None,
    ) -> list[str]:
        out_dir = (root or CANONICAL_TABLE_ROOT) / f"{content_hash[:12]}_{start_page}-{end_page}"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for table in tables:
            path = out_dir / f"{table.table_id}.json"
            path.write_text(json.dumps(table.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            rel = str(path.relative_to(PROJECT_ROOT))
            paths.append(rel)
            logger.info("canonical_table_written", table_id=table.table_id, path=rel)
        index_path = out_dir / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "content_hash": content_hash,
                    "page_range": {"start": start_page, "end": end_page},
                    "tables": paths,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return paths


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
