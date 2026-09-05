"""Final production ingestion — cached Document AI + PyMuPDF recovery, no Gold writes."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select, text

from agents.structured.store import CANONICAL_GOLD_ARTIFACT_PATH
from config.logging import configure_logging, get_logger
from config.settings import get_settings
from database.models import (
    ChemicalRegistry,
    Document,
    DocumentChunk,
    DocumentPage,
    ExtractedTable,
    Formula,
    OELChemicalLimit,
    ReviewQueueItem,
    TableCell,
    ValidatedTableRow,
)
from database.session import session_scope
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.formula_generator import FormulaGoldGenerator
from goldset_generator.semantic_text_generator import SemanticTextGenerator
from goldset_generator.structural_resolver import (
    StructuralResolverResult,
    resolve_structure,
    write_validated_structure,
)
from goldset_generator.table_gold_generator import TableGoldGenerator
from ingestion.pdf_loader import compute_file_hash
from persistence.evidence_pipeline import (
    persist_document_pages,
    persist_evidence_snapshot,
    persist_extracted_formulas,
    persist_semantic_and_row_chunks,
    persist_universal_tables,
    persist_validated_rows_and_domain,
    replace_document_extraction,
    upsert_production_document,
)

logger = get_logger(__name__)

OEL_FORMAT_START = 46
OEL_FORMAT_END = 161


class _UnavailableGemini:
    def available(self) -> bool:
        return False


def _batch_ranges(start: int, end: int, batch_size: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    current = start
    while current <= end:
        batch_end = min(current + batch_size - 1, end)
        ranges.append((current, batch_end))
        current = batch_end + 1
    return ranges


def _merge_structural(acc: StructuralResolverResult, part: StructuralResolverResult) -> None:
    acc.tables.extend(part.tables)
    acc.cells.extend(part.cells)
    acc.page_detection.update(part.page_detection)
    acc.merged_cell_count += part.merged_cell_count
    acc.recovered_table_count += part.recovered_table_count
    acc.document_ai_table_count += part.document_ai_table_count
    acc.logical_row_reconstruction_count += part.logical_row_reconstruction_count
    acc.geometry_aligned_cell_count += part.geometry_aligned_cell_count
    acc.multi_cas_visual_row_split_count += part.multi_cas_visual_row_split_count


def _collect_page_assets(structural: StructuralResolverResult) -> dict[int, dict[str, Any]]:
    by_page: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"table_ids": [], "cell_texts": set(), "table_bboxes": [], "cell_bboxes": [], "table_bbox_by_id": {}}
    )
    for table in structural.tables:
        info = by_page[table.page_number]
        info["table_ids"].append(table.table_id)
        cells = [c for c in table.flat_cells() if c.bbox]
        if cells:
            xs = [float(c.bbox["x"]) for c in cells]
            ys = [float(c.bbox["y"]) for c in cells]
            x2 = [float(c.bbox["x"]) + float(c.bbox["width"]) for c in cells]
            y2 = [float(c.bbox["y"]) + float(c.bbox["height"]) for c in cells]
            bbox = {
                "x": min(xs),
                "y": min(ys),
                "width": max(x2) - min(xs),
                "height": max(y2) - min(ys),
            }
            info["table_bboxes"].append(bbox)
            info["table_bbox_by_id"][table.table_id] = bbox
        for cell in table.flat_cells():
            text = (cell.text or "").strip()
            if text:
                info["cell_texts"].add(text)
            if cell.bbox:
                info["cell_bboxes"].append(cell.bbox)
    return by_page


def extract_document(
    pdf_path: Path,
    *,
    start_page: int,
    end_page: int,
    batch_size: int,
) -> dict[str, Any]:
    processor = DocumentProcessor()
    formula_gen = FormulaGoldGenerator(gemini_client=_UnavailableGemini())
    semantic_gen = SemanticTextGenerator()
    structural = StructuralResolverResult()
    all_pages = []
    formula_records: list[dict[str, Any]] = []
    failed_batches: list[dict[str, Any]] = []

    for batch_start, batch_end in _batch_ranges(start_page, end_page, batch_size):
        logger.info("production_ingest_batch", start=batch_start, end=batch_end)
        try:
            evidence = processor.process(
                pdf_path,
                start_page=batch_start,
                end_page=batch_end,
                skip_document_ai=True,
            )
            part = resolve_structure(evidence, pdf_path)
            _merge_structural(structural, part)
            all_pages.extend(evidence.pages)
            assets = _collect_page_assets(part)
            for page in evidence.pages:
                try:
                    _approved, _review, records = formula_gen.generate_for_page(
                        page.page_number,
                        page.text or "",
                        page.paragraphs,
                        cells=[c.to_dict() for c in part.cells if c.page_number == page.page_number],
                    )
                    del _approved, _review
                    formula_records.extend(records)
                except Exception as exc:
                    logger.warning(
                        "formula_generation_failed",
                        page=page.page_number,
                        error=str(exc),
                    )
        except Exception as exc:
            logger.exception("production_ingest_batch_failed", start=batch_start, end=batch_end)
            failed_batches.append(
                {"start": batch_start, "end": batch_end, "error": str(exc)}
            )

    semantic_records: list[dict[str, Any]] = []
    assets_all = _collect_page_assets(structural)
    formula_ids_by_page: dict[int, list[str]] = defaultdict(list)
    for record in formula_records:
        page = (record.get("evidence") or {}).get("page")
        fid = record.get("formula_id")
        if page and fid:
            formula_ids_by_page[int(page)].append(fid)

    for page in all_pages:
        info = assets_all.get(page.page_number) or {}
        semantic_records.extend(
            semantic_gen.generate_for_page(
                page_number=page.page_number,
                printed_page_number=page.printed_page_number,
                paragraphs=page.paragraphs,
                document_id=compute_file_hash(pdf_path),
                source_pdf=pdf_path.name,
                table_ids=info.get("table_ids") or [],
                formula_ids=formula_ids_by_page.get(page.page_number) or [],
                cell_texts=info.get("cell_texts") or set(),
                table_bboxes=info.get("table_bboxes") or [],
                cell_bboxes=info.get("cell_bboxes") or [],
                table_bbox_by_id=info.get("table_bbox_by_id") or {},
            )
        )

    from goldset_generator.document_processor import ProcessedDocument

    processed = ProcessedDocument(
        source_pdf=pdf_path.name,
        content_hash=compute_file_hash(pdf_path),
        start_page=start_page,
        end_page=end_page,
        pages=all_pages,
        tables=list(structural.tables),
        cells=list(structural.cells),
        raw_document_ai={},
        processor_format="cached_or_pymupdf",
    )
    return {
        "processed": processed,
        "structural": structural,
        "formula_records": formula_records,
        "semantic_records": semantic_records,
        "failed_batches": failed_batches,
    }


def _write_structures(processed, structural, start_page: int, end_page: int) -> dict[str, str]:
    settings = get_settings()
    full_path = settings.data_intermediate_dir / f"validated_structure_{start_page}-{end_page}.json"
    full_payload = {
        "schema_version": "layer2_validated_structure_v2",
        "source_pdf": processed.source_pdf,
        "content_hash": processed.content_hash,
        "pages_range": {"start": start_page, "end": end_page},
        "document_ai_table_count": structural.document_ai_table_count,
        "recovered_table_count": structural.recovered_table_count,
        "merged_cell_count": structural.merged_cell_count,
        "logical_row_reconstruction_count": structural.logical_row_reconstruction_count,
        "multi_cas_visual_row_split_count": structural.multi_cas_visual_row_split_count,
        "geometry_aligned_cell_count": structural.geometry_aligned_cell_count,
        "page_detection": structural.page_detection,
        "tables": [table.to_dict() for table in structural.tables],
        "cells": [cell.to_dict() for cell in structural.cells],
    }
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(json.dumps(full_payload, ensure_ascii=False), encoding="utf-8")

    oel_end = min(OEL_FORMAT_END, end_page)
    oel_tables = [t for t in structural.tables if OEL_FORMAT_START <= t.page_number <= oel_end]
    oel_cells = [c for c in structural.cells if OEL_FORMAT_START <= c.page_number <= oel_end]
    from goldset_generator.document_processor import ProcessedDocument
    from goldset_generator.structural_resolver import StructuralResolverResult as SR

    oel_structural = SR(
        tables=oel_tables,
        cells=oel_cells,
        page_detection={
            k: v
            for k, v in structural.page_detection.items()
            if OEL_FORMAT_START <= int(k) <= oel_end
        },
    )
    oel_processed = ProcessedDocument(
        source_pdf=processed.source_pdf,
        content_hash=processed.content_hash,
        start_page=OEL_FORMAT_START,
        end_page=oel_end,
        pages=[p for p in processed.pages if OEL_FORMAT_START <= p.page_number <= oel_end],
        tables=oel_tables,
        cells=oel_cells,
        raw_document_ai={},
        processor_format=processed.processor_format,
    )
    oel_path = write_validated_structure(
        oel_structural,
        oel_processed,
        start_page=OEL_FORMAT_START,
        end_page=oel_end,
    )
    band_46_55 = None
    if start_page <= 55 and end_page >= 46:
        slice_tables = [t for t in structural.tables if 46 <= t.page_number <= 55]
        slice_cells = [c for c in structural.cells if 46 <= c.page_number <= 55]
        slice_structural = SR(
            tables=slice_tables,
            cells=slice_cells,
            page_detection={
                k: v for k, v in structural.page_detection.items() if 46 <= int(k) <= 55
            },
        )
        slice_processed = ProcessedDocument(
            source_pdf=processed.source_pdf,
            content_hash=processed.content_hash,
            start_page=46,
            end_page=55,
            pages=[p for p in processed.pages if 46 <= p.page_number <= 55],
            tables=slice_tables,
            cells=slice_cells,
            raw_document_ai={},
            processor_format=processed.processor_format,
        )
        band_46_55 = str(
            write_validated_structure(slice_structural, slice_processed, start_page=46, end_page=55)
        )
    return {"full": str(full_path), "oel_band": str(oel_path), "pages_46_55": band_46_55}


def validate_extraction(processed, structural, *, start_page: int, end_page: int) -> dict[str, Any]:
    pages_requested = list(range(start_page, end_page + 1))
    detected_pages = set(structural.page_detection)
    table_pages = {t.page_number for t in structural.tables}
    cells = list(structural.cells)
    non_empty = [c for c in cells if str(c.text or "").strip()]
    empty = [c for c in cells if not str(c.text or "").strip()]
    with_bbox = [c for c in non_empty if c.bbox]
    oel_tables = [t for t in structural.tables if str(getattr(t, "table_type", "")) in {"chemical_oel", "TableType.CHEMICAL_OEL"} or t.table_type == "chemical_oel"]
    skipped = []
    for page_num in pages_requested:
        detection = structural.page_detection.get(page_num) or {}
        status = detection.get("table_detection_status")
        if page_num not in table_pages:
            skipped.append(
                {
                    "page": page_num,
                    "reason": status or "no_table",
                    "oel_format_band": OEL_FORMAT_START <= page_num <= OEL_FORMAT_END,
                }
            )
    return {
        "pages_requested": len(pages_requested),
        "pages_in_detection": len(detected_pages),
        "pages_with_tables": len(table_pages),
        "pages_without_tables": skipped,
        "tables": len(structural.tables),
        "oel_tables": len(oel_tables),
        "cells": len(cells),
        "non_empty_cells": len(non_empty),
        "empty_cells": len(empty),
        "bbox_coverage_non_empty": round(len(with_bbox) / len(non_empty), 4) if non_empty else 1.0,
        "document_ai_table_count": structural.document_ai_table_count,
        "recovered_table_count": structural.recovered_table_count,
        "logical_row_reconstruction_count": structural.logical_row_reconstruction_count,
        "multi_cas_visual_row_split_count": structural.multi_cas_visual_row_split_count,
        "geometry_aligned_cell_count": structural.geometry_aligned_cell_count,
    }


def validate_stel_twa(cwd: Path) -> dict[str, Any]:
    gold_xlsx = cwd / "goldset.xlsx"
    actual = cwd / "data" / "intermediate" / "validated_structure_46-55.json"
    if not gold_xlsx.exists() or not actual.exists():
        return {
            "status": "skipped",
            "reason": "goldset.xlsx or validated_structure_46-55.json missing",
            "goldset_present": gold_xlsx.exists(),
            "actual_present": actual.exists(),
        }
    import importlib.util
    import os

    previous = os.getcwd()
    os.chdir(cwd)
    try:
        spec = importlib.util.spec_from_file_location(
            "test_goldset_excel_46_55",
            cwd / "tests" / "test_goldset_excel_46_55.py",
        )
        if spec is None or spec.loader is None:
            return {"status": "skipped", "reason": "unable_to_load_stel_twa_comparator"}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        gold_rows = mod.load_excel_gold()
        actual_rows = mod.extract_actual_rows(mod.load_actual())
        stel_ok = stel_total = twa_ok = twa_total = 0
        for gold_row in gold_rows:
            actual_row = mod.find_actual_row(gold_row, actual_rows)
            if actual_row is None:
                continue
            stel_total += 1
            twa_total += 1
            if mod.compare_field("STEL", gold_row.get("STEL"), actual_row.get("STEL")):
                stel_ok += 1
            if mod.compare_field("TWA", gold_row.get("TWA"), actual_row.get("TWA")):
                twa_ok += 1
        return {
            "status": "compared",
            "stel_accuracy": round(100 * stel_ok / stel_total, 2) if stel_total else None,
            "twa_accuracy": round(100 * twa_ok / twa_total, 2) if twa_total else None,
            "stel_ok": stel_ok,
            "stel_total": stel_total,
            "twa_ok": twa_ok,
            "twa_total": twa_total,
        }
    finally:
        os.chdir(previous)


def persist_all(
    pdf_path: Path,
    processed,
    structural,
    table_golds: list[dict[str, Any]],
    formula_records: list[dict[str, Any]],
    semantic_records: list[dict[str, Any]],
    *,
    start_page: int,
    end_page: int,
) -> dict[str, Any]:
    settings = get_settings()
    content_hash = compute_file_hash(pdf_path)
    with session_scope() as session:
        document = upsert_production_document(
            session,
            filename=pdf_path.name,
            content_hash=content_hash,
            page_count=end_page - start_page + 1,
            processing_version=settings.goldset_pipeline_version,
        )
        replace_document_extraction(
            session, document, page_start=start_page, page_end=end_page
        )
        raw_path = settings.data_intermediate_dir / f"validated_structure_{start_page}-{end_page}.json"
        persist_evidence_snapshot(
            session,
            document,
            page_start=start_page,
            page_end=end_page,
            raw_json_path=raw_path,
            processor_format=processed.processor_format,
        )
        page_count = persist_document_pages(
            session, document, processed.pages, structural.page_detection
        )
        stable_map = persist_universal_tables(
            session, document, structural, structural.page_detection
        )
        domain_stats = persist_validated_rows_and_domain(
            session, document, table_golds, stable_map
        )
        formula_stats = persist_extracted_formulas(
            session, document, formula_records, stable_to_uuid=stable_map
        )
        chunk_stats = persist_semantic_and_row_chunks(
            session,
            document,
            semantic_records=semantic_records,
            table_golds=table_golds,
        )
        session.execute(text("ANALYZE document_pages"))
        session.execute(text("ANALYZE extracted_tables"))
        session.execute(text("ANALYZE table_cells"))
        session.execute(text("ANALYZE oel_chemical_limits"))
        session.execute(text("ANALYZE document_chunks"))
        session.execute(text("ANALYZE formulas"))
        return {
            "document_id": str(document.id),
            "pages": page_count,
            "tables": len(stable_map),
            **domain_stats,
            **formula_stats,
            **chunk_stats,
        }


def db_integrity(document_id: str) -> dict[str, Any]:
    with session_scope() as session:
        doc = session.get(Document, __import__("uuid").UUID(document_id))
        if not doc:
            return {"error": "document_missing"}
        cells = session.scalar(select(func.count()).select_from(TableCell).join(ExtractedTable).where(ExtractedTable.document_id == doc.id)) or 0
        cells_bbox = session.scalar(
            select(func.count())
            .select_from(TableCell)
            .join(ExtractedTable)
            .where(ExtractedTable.document_id == doc.id, TableCell.bbox.is_not(None), TableCell.raw_text.is_not(None))
        ) or 0
        empty_cells = session.scalar(
            select(func.count())
            .select_from(TableCell)
            .join(ExtractedTable)
            .where(
                ExtractedTable.document_id == doc.id,
                func.coalesce(func.nullif(func.trim(TableCell.raw_text), ""), "") == "",
            )
        ) or 0
        dup_cells = session.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                  SELECT table_id, row_index, column_index
                  FROM table_cells
                  GROUP BY table_id, row_index, column_index
                  HAVING COUNT(*) > 1
                ) d
                """
            )
        ).scalar() or 0
        dup_oel = session.execute(
            text(
                """
                SELECT COUNT(*) FROM (
                  SELECT chemical_id, source_row_key
                  FROM oel_chemical_limits
                  WHERE source_row_key IS NOT NULL
                  GROUP BY chemical_id, source_row_key
                  HAVING COUNT(*) > 1
                ) d
                """
            )
        ).scalar() or 0
        orphan_oel = session.scalar(
            select(func.count()).select_from(OELChemicalLimit).where(
                OELChemicalLimit.source_table_id.is_not(None),
                ~OELChemicalLimit.source_table_id.in_(select(ExtractedTable.id)),
            )
        ) or 0
        orphan_cells = session.scalar(
            select(func.count()).select_from(TableCell).where(
                ~TableCell.table_id.in_(select(ExtractedTable.id))
            )
        ) or 0
        chunks_total = session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc.id)) or 0
        chunks_embedded = session.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc.id,
                DocumentChunk.embedding.is_not(None),
            )
        ) or 0
        canonical_oel = session.scalar(
            select(func.count()).select_from(OELChemicalLimit).where(
                OELChemicalLimit.gold_artifact_path == CANONICAL_GOLD_ARTIFACT_PATH,
                OELChemicalLimit.validation_status == "accepted",
            )
        ) or 0
        return {
            "documents": 1,
            "document_pages": session.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.document_id == doc.id)) or 0,
            "extracted_tables": session.scalar(select(func.count()).select_from(ExtractedTable).where(ExtractedTable.document_id == doc.id)) or 0,
            "table_cells": cells,
            "validated_table_rows": session.scalar(select(func.count()).select_from(ValidatedTableRow).where(ValidatedTableRow.document_id == doc.id)) or 0,
            "chemical_registry": session.scalar(select(func.count()).select_from(ChemicalRegistry)) or 0,
            "oel_chemical_limits": session.scalar(select(func.count()).select_from(OELChemicalLimit)) or 0,
            "canonical_oel_limits": canonical_oel,
            "formulas": session.scalar(select(func.count()).select_from(Formula).where(Formula.document_id == doc.id)) or 0,
            "document_chunks": chunks_total,
            "document_chunks_embedded": chunks_embedded,
            "review_queue": session.scalar(select(func.count()).select_from(ReviewQueueItem).where(ReviewQueueItem.document_id == doc.id)) or 0,
            "empty_cells": empty_cells,
            "cells_with_bbox": cells_bbox,
            "duplicate_cell_positions": dup_cells,
            "duplicate_oel_keys": dup_oel,
            "orphan_oel_limits": orphan_oel,
            "orphan_cells": orphan_cells,
        }


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Final production ingestion for OHE6")
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT.parent / "OHE6.pdf")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--skip-persist", action="store_true")
    args = parser.parse_args()

    pdf_path = args.file.resolve()
    import fitz

    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count
    start_page = args.start_page
    end_page = args.end_page or total_pages

    extracted = extract_document(
        pdf_path, start_page=start_page, end_page=end_page, batch_size=args.batch_size
    )
    processed = extracted["processed"]
    structural = extracted["structural"]
    paths = _write_structures(processed, structural, start_page, end_page)
    table_gold_gen = TableGoldGenerator()
    table_golds = [table_gold_gen.generate(t.to_dict()) for t in structural.tables]
    extraction_report = validate_extraction(
        processed, structural, start_page=start_page, end_page=end_page
    )
    stel_twa = validate_stel_twa(PROJECT_ROOT)

    persist_stats: dict[str, Any] = {"skipped": True}
    integrity: dict[str, Any] = {}
    if not args.skip_persist:
        persist_stats = persist_all(
            pdf_path,
            processed,
            structural,
            table_golds,
            extracted["formula_records"],
            extracted["semantic_records"],
            start_page=start_page,
            end_page=end_page,
        )
        embed_stats: dict[str, Any] = {"skipped": True}
        try:
            from persistence.semantic_store import embed_pending_production_chunks
            from retrieval.embeddings import EmbeddingService

            if EmbeddingService().available():
                with session_scope() as session:
                    embed_stats = embed_pending_production_chunks(session, batch_size=8, commit_every=1).to_dict()
            else:
                embed_stats = {"error": "embedding_service_unavailable"}
        except Exception as exc:
            embed_stats = {"error": str(exc)}
        persist_stats["embeddings"] = embed_stats
        integrity = db_integrity(persist_stats["document_id"])

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdf": str(pdf_path),
        "pages": f"{start_page}-{end_page}",
        "document_ai_api_calls": 0,
        "gold_files_modified": False,
        "structure_paths": paths,
        "failed_batches": extracted["failed_batches"],
        "extraction": extraction_report,
        "stel_twa": stel_twa,
        "formulas_extracted": len(extracted["formula_records"]),
        "semantic_records": len(extracted["semantic_records"]),
        "persist": persist_stats,
        "db_integrity": integrity,
        "retrieval_readiness": {
            "canonical_oel": integrity.get("canonical_oel_limits"),
            "chunks": integrity.get("document_chunks"),
            "embeddings": integrity.get("document_chunks_embedded"),
            "vector_index": "sequential_scan_3072d_per_migration_005",
            "evidence_trace": "table_cells.bbox + validated_table_rows.field_provenance + chunk.provenance",
        },
    }
    out = PROJECT_ROOT / "data" / "intermediate" / "production_ingestion_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if extracted["failed_batches"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
