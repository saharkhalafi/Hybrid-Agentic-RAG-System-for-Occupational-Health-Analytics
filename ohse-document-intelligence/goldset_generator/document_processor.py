"""Layer 1 evidence extraction ΓÇö Document AI Layout Parser only."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from config.logging import get_logger
from config.settings import get_settings
from document_ai.parser import parse_document_ai_response
from document_ai.processor import DocumentAIProcessor
from document_ai.schema import DocumentAIExtractionResult
from ingestion.page_numbers import extract_printed_page_number
from ingestion.pdf_loader import load_pdf
from normalization.persian_normalizer import normalize_persian_text
from validation.structural_confidence import compute_structural_confidence

logger = get_logger(__name__)


def _cell_id(table_id: str, row: int, col: int) -> str:
    return f"cell_{table_id}_{row}_{col}"


def _table_id(page_number: int, index: int) -> str:
    return f"table_{page_number:03d}_{index:02d}"


@dataclass
class ExtractedCellRecord:
    cell_id: str
    table_id: str
    page_number: int
    row: int
    column: int
    text: str
    bbox: dict[str, Any] | None
    confidence: float | None
    bbox_confidence: float | None
    bbox_source: str | None
    source: str
    normalized_value: str | None = None
    source_reference: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "table_id": self.table_id,
            "page_number": self.page_number,
            "row": self.row,
            "column": self.column,
            "text": self.text,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "bbox_confidence": self.bbox_confidence,
            "bbox_source": self.bbox_source,
            "source": self.source,
            "normalized_value": self.normalized_value,
            "source_reference": self.source_reference,
        }


@dataclass
class ExtractedTableRecord:
    table_id: str
    page_number: int
    table_type: str
    rows: list[list[ExtractedCellRecord]]
    structural_confidence: float | None
    raw_markdown: str
    bbox: dict[str, Any] | None = None

    def flat_cells(self) -> list[ExtractedCellRecord]:
        return [cell for row in self.rows for cell in row]

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "page_number": self.page_number,
            "table_type": self.table_type,
            "structural_confidence": self.structural_confidence,
            "raw_markdown": self.raw_markdown,
            "bbox": self.bbox,
            "rows": [[cell.to_dict() for cell in row] for row in self.rows],
            "cells": [cell.to_dict() for cell in self.flat_cells()],
        }


@dataclass
class PageTextRecord:
    page_number: int
    text: str
    has_digital_text: bool
    printed_page_number: int | None = None
    paragraphs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "pdf_page_number": self.page_number,
            "printed_page_number": self.printed_page_number,
            "text": self.text,
            "has_digital_text": self.has_digital_text,
            "paragraphs": self.paragraphs,
        }


@dataclass
class ProcessedDocument:
    source_pdf: str
    content_hash: str
    start_page: int
    end_page: int
    pages: list[PageTextRecord]
    tables: list[ExtractedTableRecord]
    cells: list[ExtractedCellRecord]
    raw_document_ai: dict[str, Any]
    processor_format: str
    page_table_detection: dict[int, dict[str, Any]] = field(default_factory=dict)

    def cells_by_page(self, page_number: int) -> list[ExtractedCellRecord]:
        return [cell for cell in self.cells if cell.page_number == page_number]

    def tables_by_page(self, page_number: int) -> list[ExtractedTableRecord]:
        return [table for table in self.tables if table.page_number == page_number]

    def to_intermediate_dict(self) -> dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "content_hash": self.content_hash,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "processor_format": self.processor_format,
            "pages": [page.to_dict() for page in self.pages],
            "tables": [table.to_dict() for table in self.tables],
            "cells": [cell.to_dict() for cell in self.cells],
        }


def _find_all_cached_document_ai_raw(
    base_hash: str, start_page: int, end_page: int
) -> list[tuple[int, int, Path]]:
    """Locate all cached Document AI raw JSON files overlapping the requested range."""
    settings = get_settings()
    prefix = base_hash[:12]
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)-(\d+)_document_ai\.json$")
    candidates: list[tuple[int, int, Path]] = []
    for path in settings.data_processed_dir.glob(f"{prefix}*_document_ai.json"):
        match = pattern.match(path.name)
        if not match:
            continue
        file_start, file_end = int(match.group(1)), int(match.group(2))
        if file_start <= end_page and file_end >= start_page:
            candidates.append((file_start, file_end, path))
    return candidates


def _find_cached_document_ai_raw(base_hash: str, start_page: int, end_page: int) -> Path | None:
    """Locate cached Document AI raw JSON overlapping the requested page range."""
    candidates = _find_all_cached_document_ai_raw(base_hash, start_page, end_page)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[1] - item[0], -item[0]))
    return candidates[0][2]


def _best_cache_for_page(
    page_number: int,
    caches: list[tuple[int, int, Path]],
) -> tuple[int, int, Path] | None:
    matching = [item for item in caches if item[0] <= page_number <= item[1]]
    if not matching:
        return None
    matching.sort(key=lambda item: (item[1] - item[0], -item[0]))
    return matching[0]


class DocumentProcessor:
    """Layer 1 ΓÇö extract immutable evidence from Document AI Layout Parser."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _write_page_subset(
        self,
        source: Path,
        destination: Path,
        page_start: int,
        page_end: int,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(source) as src:
            dst = fitz.open()
            start_index = page_start - 1
            end_index = page_end - 1
            dst.insert_pdf(src, from_page=start_index, to_page=end_index)
            dst.save(destination)

    def _offset_pages(self, parsed: DocumentAIExtractionResult, page_start: int) -> None:
        if page_start <= 1:
            return
        offset = page_start - 1
        for page in parsed.pages:
            page.page_number += offset
            for table in page.tables:
                table.page_number += offset
                for cell in table.flat_cells():
                    if cell.page_number is not None:
                        cell.page_number += offset

    def _extract_paragraphs(self, pdf_path: Path, page_number: int) -> list[dict[str, Any]]:
        doc = fitz.open(pdf_path)
        page = doc.load_page(page_number - 1)
        paragraphs: list[dict[str, Any]] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = []
            sizes: list[float] = []
            for line in block.get("lines", []):
                line_text = "".join(span.get("text", "") for span in line.get("spans", []))
                lines.append(line_text)
                sizes.extend(float(span.get("size") or 0) for span in line.get("spans", []))
            text = "\n".join(lines).strip()
            if not text:
                continue
            bbox = block.get("bbox")
            paragraphs.append(
                {
                    "text": text,
                    "leading_indent": len(re.match(r"^(\s*)", text).group(1)) if re.match(r"^(\s*)", text) else 0,
                    "max_font_size": max(sizes) if sizes else None,
                    "min_font_size": min(s for s in sizes if s > 0) if sizes else None,
                    "line_count": len(lines),
                    "bbox": (
                        {"x": bbox[0], "y": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]}
                        if bbox
                        else None
                    ),
                }
            )
        doc.close()
        return paragraphs

    def _build_evidence_records(
        self,
        parsed: DocumentAIExtractionResult,
        table_classifier,
    ) -> tuple[list[ExtractedTableRecord], list[ExtractedCellRecord]]:
        """Build Layer 1 evidence cells from Document AI ΓÇö no geometry alignment."""
        tables: list[ExtractedTableRecord] = []
        all_cells: list[ExtractedCellRecord] = []
        table_index_by_page: dict[int, int] = {}

        for table in parsed.tables:
            page_number = table.page_number
            table_index_by_page[page_number] = table_index_by_page.get(page_number, 0) + 1
            idx = table_index_by_page[page_number]
            tid = _table_id(page_number, idx)
            table_type = table_classifier(table.raw_markdown).value

            row_records: list[list[ExtractedCellRecord]] = []
            for row in table.rows:
                cell_row: list[ExtractedCellRecord] = []
                for cell in row.cells:
                    normalized = normalize_persian_text(cell.text) if cell.text else None
                    cid = _cell_id(tid, cell.row_index, cell.column_index)
                    record = ExtractedCellRecord(
                        cell_id=cid,
                        table_id=tid,
                        page_number=cell.page_number or page_number,
                        row=cell.row_index,
                        column=cell.column_index,
                        text=cell.text,
                        bbox=cell.bbox,
                        confidence=cell.confidence,
                        bbox_confidence=cell.confidence if cell.bbox else None,
                        bbox_source="document_ai" if cell.bbox else None,
                        source="document_ai",
                        normalized_value=normalized.normalized if normalized else None,
                        source_reference={
                            "page_number": page_number,
                            "cell_ids": [cid],
                            "bbox": cell.bbox,
                            "row_span": cell.row_span,
                            "column_span": cell.column_span,
                        },
                    )
                    cell_row.append(record)
                    all_cells.append(record)
                row_records.append(cell_row)

            structural = compute_structural_confidence(table.rows)
            tables.append(
                ExtractedTableRecord(
                    table_id=tid,
                    page_number=page_number,
                    table_type=table_type,
                    rows=row_records,
                    structural_confidence=structural,
                    raw_markdown=table.raw_markdown,
                    bbox=table.bbox,
                )
            )

        return tables, all_cells

    def _layer1_from_merged_caches(
        self,
        caches: list[tuple[int, int, Path]],
        *,
        start_page: int,
        end_page: int,
        table_classifier,
    ) -> tuple[list[ExtractedTableRecord], list[ExtractedCellRecord], dict[str, Any], str]:
        """Load Layer 1 tables from every overlapping Document AI cache without an API call."""
        page_to_cache: dict[int, tuple[int, int, Path]] = {}
        for page_number in range(start_page, end_page + 1):
            chosen = _best_cache_for_page(page_number, caches)
            if chosen is not None:
                page_to_cache[page_number] = chosen

        by_path: dict[Path, tuple[int, int, set[int]]] = {}
        for page_number, (file_start, file_end, path) in page_to_cache.items():
            if path not in by_path:
                by_path[path] = (file_start, file_end, set())
            by_path[path][2].add(page_number)

        tables: list[ExtractedTableRecord] = []
        cells: list[ExtractedCellRecord] = []
        merged_raw: dict[str, Any] = {"caches": []}
        processor_format = "unknown"

        for path, (file_start, _file_end, pages) in by_path.items():
            raw_json = json.loads(path.read_text(encoding="utf-8"))
            parsed = parse_document_ai_response(raw_json)
            self._offset_pages(parsed, file_start)
            processor_format = parsed.processor_format or processor_format
            page_tables, page_cells = self._build_evidence_records(parsed, table_classifier)
            tables.extend(t for t in page_tables if t.page_number in pages)
            cells.extend(c for c in page_cells if c.page_number in pages)
            merged_raw["caches"].append(
                {
                    "path": str(path),
                    "page_start": file_start,
                    "pages": sorted(pages),
                }
            )
            logger.info(
                "loaded_cached_document_ai_raw",
                path=str(path),
                cache_start=file_start,
                pages=len(pages),
            )

        return tables, cells, merged_raw, processor_format

    def process(
        self,
        pdf_path: Path,
        *,
        start_page: int,
        end_page: int,
        skip_document_ai: bool = False,
        cached_raw_json: Path | None = None,
    ) -> ProcessedDocument:
        from knowledge.table_classifier import classify_table_text

        loaded = load_pdf(pdf_path, page_start=start_page, page_end=end_page)
        content_hash = hashlib.sha256(
            f"{loaded.content_hash}:{start_page}-{end_page}".encode()
        ).hexdigest()

        raw_json: dict[str, Any] = {}
        parsed: DocumentAIExtractionResult | None = None
        tables: list[ExtractedTableRecord] = []
        cells: list[ExtractedCellRecord] = []
        processor_format = "unknown"

        if cached_raw_json and cached_raw_json.exists():
            raw_json = json.loads(cached_raw_json.read_text(encoding="utf-8"))
            parsed = parse_document_ai_response(raw_json)
            self._offset_pages(parsed, start_page)
        else:
            caches = _find_all_cached_document_ai_raw(loaded.content_hash, start_page, end_page)
            if caches:
                tables, cells, raw_json, processor_format = self._layer1_from_merged_caches(
                    caches,
                    start_page=start_page,
                    end_page=end_page,
                    table_classifier=classify_table_text,
                )
            elif not skip_document_ai:
                subset_path = (
                    self.settings.data_intermediate_dir
                    / f"{loaded.content_hash[:12]}_{start_page}-{end_page}.pdf"
                )
                self._write_page_subset(pdf_path, subset_path, start_page, end_page)
                processor = DocumentAIProcessor()
                parsed, raw_json = processor.process_pdf(subset_path)
                self._offset_pages(parsed, start_page)

        raw_path = (
            self.settings.data_intermediate_dir / f"document_ai_raw_{start_page}-{end_page}.json"
        )
        if raw_json:
            raw_path.write_text(json.dumps(raw_json, ensure_ascii=False), encoding="utf-8")

        intermediate_path = self.settings.data_intermediate_dir / "document_ai_result.json"
        intermediate_path.parent.mkdir(parents=True, exist_ok=True)

        pages: list[PageTextRecord] = []
        pdf_doc = fitz.open(pdf_path)
        for page_content in loaded.pages:
            pdf_page = pdf_doc.load_page(page_content.page_number - 1)
            paragraphs = self._extract_paragraphs(pdf_path, page_content.page_number)
            printed = extract_printed_page_number(pdf_page)
            pages.append(
                PageTextRecord(
                    page_number=page_content.page_number,
                    text=page_content.text,
                    has_digital_text=page_content.has_digital_text,
                    printed_page_number=printed,
                    paragraphs=paragraphs,
                )
            )
        pdf_doc.close()

        if parsed:
            processor_format = parsed.processor_format
            tables, cells = self._build_evidence_records(parsed, classify_table_text)
            tables = [t for t in tables if start_page <= t.page_number <= end_page]
            cells = [c for c in cells if start_page <= c.page_number <= end_page]

        result = ProcessedDocument(
            source_pdf=pdf_path.name,
            content_hash=content_hash,
            start_page=start_page,
            end_page=end_page,
            pages=pages,
            tables=tables,
            cells=cells,
            raw_document_ai=raw_json,
            processor_format=processor_format,
        )

        page_scoped = {
            **result.to_intermediate_dict(),
            "pages_range": {"start": start_page, "end": end_page},
            "raw_document_ai_included": bool(raw_json),
        }
        intermediate_path.write_text(json.dumps(page_scoped, ensure_ascii=False, indent=2), encoding="utf-8")
        range_path = (
            self.settings.data_intermediate_dir
            / f"document_ai_result_{start_page}-{end_page}.json"
        )
        range_path.write_text(json.dumps(page_scoped, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("stored_intermediate_extraction", path=str(range_path), cells=len(cells), tables=len(tables))

        return result
