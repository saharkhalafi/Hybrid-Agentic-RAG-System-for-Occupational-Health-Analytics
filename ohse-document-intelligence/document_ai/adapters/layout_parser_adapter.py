"""Adapter for Google Document AI Layout Parser (documentLayout) output."""

from __future__ import annotations

import uuid
from typing import Any

from document_ai.adapters.bbox import (
    extract_bbox_from_block,
    extract_confidence_from_layout,
    page_span_reference,
)
from document_ai.schema import (
    DocumentAIExtractionResult,
    ExtractionCell,
    ExtractionPage,
    ExtractionRow,
    ExtractionTable,
)
from validation.structural_confidence import compute_structural_confidence


def _cell_text_from_blocks(blocks: list[dict[str, Any]]) -> tuple[str, list[str], dict[str, Any] | None, float | None]:
    texts: list[str] = []
    block_ids: list[str] = []
    bbox: dict[str, Any] | None = None
    confidence: float | None = None

    for block in blocks:
        block_id = block.get("blockId")
        if block_id:
            block_ids.append(str(block_id))

        text_block = block.get("textBlock") or {}
        text = text_block.get("text", "")
        if text:
            texts.append(text)

        if bbox is None:
            bbox = extract_bbox_from_block(block)

        if confidence is None:
            layout = block.get("layout") or text_block.get("layout")
            confidence = extract_confidence_from_layout(layout)

    return " ".join(texts).strip(), block_ids, bbox, confidence


def _parse_table_block(
    table_block: dict[str, Any],
    page_number: int,
    table_block_id: str | None,
    table_page_span: dict[str, Any] | None,
) -> ExtractionTable:
    header_rows = table_block.get("headerRows") or table_block.get("header_rows") or []
    body_rows = table_block.get("bodyRows") or table_block.get("body_rows") or []
    all_rows = list(header_rows) + list(body_rows)

    rows: list[ExtractionRow] = []
    ocr_confidences: list[float] = []

    for row_index, row in enumerate(all_rows):
        extraction_row = ExtractionRow()
        for column_index, cell in enumerate(row.get("cells") or []):
            text, block_ids, bbox, confidence = _cell_text_from_blocks(cell.get("blocks") or [])
            if confidence is not None:
                ocr_confidences.append(confidence)

            extraction_row.cells.append(
                ExtractionCell(
                    text=text,
                    bbox=bbox,
                    confidence=confidence,
                    row_index=row_index,
                    column_index=column_index,
                    row_span=int(cell.get("rowSpan") or cell.get("row_span") or 1),
                    column_span=int(cell.get("colSpan") or cell.get("col_span") or 1),
                    block_ids=block_ids,
                    page_number=page_number,
                )
            )
        rows.append(extraction_row)

    table_bbox = extract_bbox_from_block(table_block) or page_span_reference(table_page_span)
    ocr_confidence = sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else None
    structural_confidence = compute_structural_confidence(rows)

    return ExtractionTable(
        table_id=table_block_id or str(uuid.uuid4()),
        page_number=page_number,
        bbox=table_bbox if extract_bbox_from_block(table_block) else None,
        rows=rows,
        raw_json=table_block,
        ocr_confidence=ocr_confidence,
        structural_confidence=structural_confidence,
    )


def _walk_blocks(
    blocks: list[dict[str, Any]],
    pages: dict[int, ExtractionPage],
    page_hint: int | None = None,
) -> None:
    for block in blocks:
        page_span = block.get("pageSpan") or {}
        page_number = page_span.get("pageStart") or page_span.get("page_start") or page_hint or 1

        if page_number not in pages:
            pages[page_number] = ExtractionPage(page_number=page_number, text="")

        if "tableBlock" in block:
            table = _parse_table_block(
                block["tableBlock"],
                page_number=page_number,
                table_block_id=str(block.get("blockId")) if block.get("blockId") else None,
                table_page_span=page_span,
            )
            pages[page_number].tables.append(table)
            continue

        text_block = block.get("textBlock") or {}
        text = text_block.get("text", "")
        if text:
            pages[page_number].text = f"{pages[page_number].text}\n{text}".strip()

        nested = text_block.get("blocks") or block.get("blocks")
        if nested:
            _walk_blocks(nested, pages, page_number)


class LayoutParserAdapter:
    processor_format = "layout_parser"

    def adapt(self, raw_json: dict[str, Any]) -> DocumentAIExtractionResult:
        document_layout = raw_json.get("documentLayout") or {}
        pages_map: dict[int, ExtractionPage] = {}
        _walk_blocks(document_layout.get("blocks") or [], pages_map)

        pages = [pages_map[key] for key in sorted(pages_map)]
        full_text = "\n".join(page.text for page in pages if page.text)

        return DocumentAIExtractionResult(
            pages=pages,
            full_text=full_text,
            raw_json=raw_json,
            processor_format=self.processor_format,
        )
