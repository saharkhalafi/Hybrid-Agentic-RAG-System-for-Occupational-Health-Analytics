"""Normalized Document AI extraction schema — pipeline must use this, not Google JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractionCell:
    text: str
    bbox: dict[str, Any] | None
    confidence: float | None
    row_index: int
    column_index: int
    row_span: int = 1
    column_span: int = 1
    block_ids: list[str] = field(default_factory=list)
    page_number: int | None = None


@dataclass
class ExtractionRow:
    cells: list[ExtractionCell] = field(default_factory=list)


@dataclass
class ExtractionTable:
    table_id: str
    page_number: int
    bbox: dict[str, Any] | None
    rows: list[ExtractionRow] = field(default_factory=list)
    title: str | None = None
    caption: str | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)
    ocr_confidence: float | None = None
    structural_confidence: float | None = None

    @property
    def raw_markdown(self) -> str:
        lines: list[str] = []
        for row in self.rows:
            lines.append("| " + " | ".join(c.text.replace("|", "\\|") for c in row.cells) + " |")
        return "\n".join(lines)

    def flat_cells(self) -> list[ExtractionCell]:
        return [cell for row in self.rows for cell in row.cells]


@dataclass
class ExtractionPage:
    page_number: int
    text: str
    tables: list[ExtractionTable] = field(default_factory=list)
    layout_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentAIExtractionResult:
    pages: list[ExtractionPage]
    full_text: str
    raw_json: dict[str, Any]
    processor_format: str

    @property
    def tables(self) -> list[ExtractionTable]:
        return [table for page in self.pages for table in page.tables]
