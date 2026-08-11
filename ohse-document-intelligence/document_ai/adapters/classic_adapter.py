"""Adapter for classic Document AI output (pages[].tables[])."""

from __future__ import annotations

import uuid
from typing import Any

from document_ai.adapters.bbox import (
    extract_bbox_from_layout,
    extract_confidence_from_layout,
    extract_text_from_anchor,
)
from document_ai.schema import (
    DocumentAIExtractionResult,
    ExtractionCell,
    ExtractionPage,
    ExtractionRow,
    ExtractionTable,
)
from validation.structural_confidence import compute_structural_confidence


def _parse_classic_table(text: str, table_block: dict[str, Any], page_number: int) -> ExtractionTable:
    header_rows = table_block.get("headerRows") or table_block.get("header_rows") or []
    body_rows = table_block.get("bodyRows") or table_block.get("body_rows") or []
    all_rows = list(header_rows) + list(body_rows)

    rows: list[ExtractionRow] = []
    ocr_confidences: list[float] = []

    for row_index, row in enumerate(all_rows):
        extraction_row = ExtractionRow()
        for column_index, cell in enumerate(row.get("cells") or []):
            layout = cell.get("layout") or {}
            cell_text = extract_text_from_anchor(text, layout.get("textAnchor") or layout.get("text_anchor"))
            confidence = extract_confidence_from_layout(layout)
            if confidence is not None:
                ocr_confidences.append(confidence)

            extraction_row.cells.append(
                ExtractionCell(
                    text=cell_text,
                    bbox=extract_bbox_from_layout(layout),
                    confidence=confidence,
                    row_index=row_index,
                    column_index=column_index,
                    row_span=int(cell.get("rowSpan") or cell.get("row_span") or 1),
                    column_span=int(cell.get("colSpan") or cell.get("col_span") or 1),
                    page_number=page_number,
                )
            )
        rows.append(extraction_row)

    ocr_confidence = sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else None

    return ExtractionTable(
        table_id=str(uuid.uuid4()),
        page_number=page_number,
        bbox=extract_bbox_from_layout(table_block.get("layout")),
        rows=rows,
        raw_json=table_block,
        ocr_confidence=ocr_confidence,
        structural_confidence=compute_structural_confidence(rows),
    )


class ClassicDocumentAIAdapter:
    processor_format = "classic"

    def adapt(self, raw_json: dict[str, Any]) -> DocumentAIExtractionResult:
        full_text = raw_json.get("text", "")
        pages: list[ExtractionPage] = []

        for page_index, page in enumerate(raw_json.get("pages", []), start=1):
            extraction_page = ExtractionPage(
                page_number=page_index,
                text=full_text,
                layout_metadata={
                    "dimension": page.get("dimension"),
                    "block_count": len(page.get("blocks") or []),
                },
            )
            for table_block in page.get("tables") or []:
                extraction_page.tables.append(_parse_classic_table(full_text, table_block, page_index))
            pages.append(extraction_page)

        return DocumentAIExtractionResult(
            pages=pages,
            full_text=full_text,
            raw_json=raw_json,
            processor_format=self.processor_format,
        )
