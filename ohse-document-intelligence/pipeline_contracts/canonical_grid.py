"""Canonical Grid — parser-agnostic structural representation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import PROJECT_ROOT
from pipeline_contracts.confidence import LayeredConfidence
from pipeline_contracts.header_reconstruction import reconstruct_header_structure
from pipeline_contracts.provenance import PipelineStage, ProcessorProvenance

logger = get_logger(__name__)

CANONICAL_ROOT = PROJECT_ROOT / "data" / "canonical" / "grids"


@dataclass
class CanonicalCell:
    cell_id: str
    row: int
    column: int
    text: str
    normalized_text: str | None = None
    bbox: dict[str, Any] | None = None
    bbox_source: str | None = None
    row_span: int = 1
    column_span: int = 1
    merged: bool = False
    source: str = "document_ai"
    provenance: ProcessorProvenance | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cell_id": self.cell_id,
            "row": self.row,
            "column": self.column,
            "text": self.text,
            "normalized_text": self.normalized_text,
            "bbox": self.bbox,
            "bbox_source": self.bbox_source,
            "row_span": self.row_span,
            "column_span": self.column_span,
            "merged": self.merged,
            "source": self.source,
        }
        if self.provenance:
            payload["provenance"] = self.provenance.to_dict()
        return payload


@dataclass
class CanonicalGridRow:
    row_index: int
    cells: list[CanonicalCell] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"row_index": self.row_index, "cells": [cell.to_dict() for cell in self.cells]}


@dataclass
class CanonicalGrid:
    table_id: str
    page_number: int
    source_processors: list[str]
    rows: list[CanonicalGridRow]
    headers: list[CanonicalCell] = field(default_factory=list)
    confidence: LayeredConfidence = field(default_factory=LayeredConfidence)
    provenance: ProcessorProvenance | None = None
    evidence_refs: list[str] = field(default_factory=list)
    header_structure: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "page_number": self.page_number,
            "source_processors": self.source_processors,
            "headers": [cell.to_dict() for cell in self.headers],
            "rows": [row.to_dict() for row in self.rows],
            "confidence": self.confidence.to_dict(),
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "evidence_refs": self.evidence_refs,
            "header_structure": self.header_structure,
        }


class CanonicalGridBuilder:
    """Build CanonicalGrid from structural resolver output (any parser source)."""

    def __init__(
        self,
        *,
        pipeline_version: str,
        processor_version: str,
        evidence_refs: list[str] | None = None,
    ) -> None:
        self.pipeline_version = pipeline_version
        self.processor_version = processor_version
        self.evidence_refs = evidence_refs or []

    def from_table_dict(self, table: dict[str, Any]) -> CanonicalGrid:
        table_id = table["table_id"]
        page_number = table["page_number"]
        sources: set[str] = set()
        header_cells: list[CanonicalCell] = []
        body_rows: list[CanonicalGridRow] = []

        raw_rows = table.get("rows") or []
        for row_idx, row in enumerate(raw_rows):
            cells: list[CanonicalCell] = []
            for cell in row:
                if not isinstance(cell, dict):
                    continue
                source = str(cell.get("source") or "document_ai")
                sources.add(source)
                ref = cell.get("source_reference") or {}
                row_span = int(ref.get("row_span") or 1)
                col_span = int(ref.get("column_span") or 1)
                merged = row_span > 1 or col_span > 1 or ref.get("value_status") == "merged_cell"
                canonical = CanonicalCell(
                    cell_id=str(cell.get("cell_id") or ""),
                    row=int(cell.get("row") or row_idx),
                    column=int(cell.get("column") or 0),
                    text=str(cell.get("text") or ""),
                    normalized_text=cell.get("normalized_value"),
                    bbox=cell.get("bbox"),
                    bbox_source=cell.get("bbox_source"),
                    row_span=row_span,
                    column_span=col_span,
                    merged=merged,
                    source=source,
                    provenance=ProcessorProvenance(
                        processor=source,
                        processor_version=self.processor_version,
                        pipeline_version=self.pipeline_version,
                        pipeline_stage=PipelineStage.STRUCTURAL.value,
                        confidence_source="structural_resolver",
                        evidence_ref=self.evidence_refs[0] if self.evidence_refs else None,
                    ),
                )
                cells.append(canonical)
            if row_idx == 0 and cells:
                header_cells = cells
            else:
                body_rows.append(CanonicalGridRow(row_index=row_idx, cells=cells))

        layout_conf = _float_or_none(table.get("structural_confidence"))
        confidence = LayeredConfidence(
            layout=layout_conf,
            geometry=layout_conf,
        )

        table_type = str(table.get("table_type") or "")
        header_structure = reconstruct_header_structure(
            raw_rows,
            table_type=table_type,
        ).to_dict()

        return CanonicalGrid(
            table_id=table_id,
            page_number=page_number,
            source_processors=sorted(sources),
            rows=body_rows,
            headers=header_cells,
            confidence=confidence,
            provenance=ProcessorProvenance(
                processor="structural_resolver",
                processor_version=self.processor_version,
                pipeline_version=self.pipeline_version,
                pipeline_stage=PipelineStage.STRUCTURAL.value,
                confidence_source="canonical_grid_builder",
            ),
            evidence_refs=list(self.evidence_refs),
            header_structure=header_structure,
        )

    def build_all(self, tables: list[dict[str, Any]]) -> list[CanonicalGrid]:
        return [self.from_table_dict(table) for table in tables]

    @staticmethod
    def write_all(
        grids: list[CanonicalGrid],
        *,
        content_hash: str,
        start_page: int,
        end_page: int,
        root: Path | None = None,
    ) -> list[str]:
        out_dir = (root or CANONICAL_ROOT) / f"{content_hash[:12]}_{start_page}-{end_page}"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for grid in grids:
            path = out_dir / f"{grid.table_id}.json"
            path.write_text(json.dumps(grid.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            rel = str(path.relative_to(PROJECT_ROOT))
            paths.append(rel)
            logger.info("canonical_grid_written", table_id=grid.table_id, path=rel)
        index_path = out_dir / "index.json"
        index_path.write_text(
            json.dumps(
                {
                    "content_hash": content_hash,
                    "page_range": {"start": start_page, "end": end_page},
                    "grids": paths,
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
