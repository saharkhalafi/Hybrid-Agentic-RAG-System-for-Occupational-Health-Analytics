"""Run semantic-text preparation only — does not modify tables, formulas, entities, or pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.semantic_text_generator import SemanticTextGenerator
from goldset_generator.semantic_text_report import write_semantic_text_report
from goldset_generator.semantic_text_validator import SemanticTextValidator
from goldset_generator.structural_resolver import resolve_structure

logger = get_logger(__name__)


def _merge_jsonl_records(
    existing: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    *,
    start_page: int,
    end_page: int,
) -> list[dict[str, Any]]:
    preserved = [
        record
        for record in existing
        if not (
            record.get("pdf_page_number") is not None
            and start_page <= int(record["pdf_page_number"]) <= end_page
        )
    ]
    return preserved + new_records


def _load_page_index(gold_dir: Path, page_number: int) -> dict[str, Any]:
    path = gold_dir / "pages" / f"page_{page_number:03d}.json"
    candidate_path = gold_dir / "candidates" / "pages" / f"page_{page_number:03d}.json"
    payload: dict[str, Any] = {}
    if path.exists():
        payload.update(json.loads(path.read_text(encoding="utf-8")))
    if candidate_path.exists():
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        payload.setdefault("tables", candidate.get("tables") or [])
        payload.setdefault("formulas", candidate.get("formulas") or [])
    return payload


def _collect_table_regions(
    page_number: int,
    page_cells: list[dict[str, Any]],
    page_tables: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    cell_bboxes = [cell["bbox"] for cell in page_cells if cell.get("bbox")]
    cell_texts = {(cell.get("text") or "").strip() for cell in page_cells if (cell.get("text") or "").strip()}
    table_bboxes: list[dict[str, Any]] = []
    table_bbox_by_id: dict[str, dict[str, Any]] = {}
    for table in page_tables:
        table_id = table.get("table_id")
        cells = [c for c in page_cells if c.get("table_id") == table_id and c.get("bbox")]
        if not cells:
            continue
        xs = [float(c["bbox"]["x"]) for c in cells]
        ys = [float(c["bbox"]["y"]) for c in cells]
        x2 = [float(c["bbox"]["x"]) + float(c["bbox"]["width"]) for c in cells]
        y2 = [float(c["bbox"]["y"]) + float(c["bbox"]["height"]) for c in cells]
        bbox = {"x": min(xs), "y": min(ys), "width": max(x2) - min(xs), "height": max(y2) - min(ys)}
        table_bboxes.append(bbox)
        if table_id:
            table_bbox_by_id[table_id] = bbox
    return table_bboxes, cell_bboxes, table_bbox_by_id, cell_texts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Prepare semantic text chunks for RAG retrieval")
    parser.add_argument("--file", required=True, type=Path, help="Path to PDF")
    parser.add_argument("--start-page", type=int, required=True)
    parser.add_argument("--end-page", type=int, required=True)
    args = parser.parse_args()

    if args.start_page > args.end_page:
        raise SystemExit("--start-page must be <= --end-page")

    settings = get_settings()
    gold_dir = settings.gold_dir
    processor = DocumentProcessor()
    generator = SemanticTextGenerator()

    evidence = processor.process(
        args.file,
        start_page=args.start_page,
        end_page=args.end_page,
        skip_document_ai=True,
    )
    structural = resolve_structure(evidence, args.file)

    all_chunks: list[dict[str, Any]] = []
    page_stats: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []

    for page in evidence.pages:
        page_number = page.page_number
        page_index = _load_page_index(gold_dir, page_number)
        table_ids = page_index.get("tables") or []
        formula_ids = page_index.get("formulas") or []

        page_cells = [c.to_dict() for c in structural.cells if c.page_number == page_number]
        page_tables = [t.to_dict() for t in structural.tables if t.page_number == page_number]
        table_bboxes, cell_bboxes, table_bbox_by_id, cell_texts = _collect_table_regions(
            page_number, page_cells, page_tables
        )

        paragraph_evidence = {
            f"text_evidence_{page_number:03d}_{idx:03d}": (p.get("text") or "")
            for idx, p in enumerate(page.paragraphs)
        }
        chunks = generator.generate_for_page(
            page_number=page_number,
            printed_page_number=page.printed_page_number,
            paragraphs=page.paragraphs,
            document_id=evidence.content_hash,
            source_pdf=args.file.name,
            table_ids=table_ids,
            formula_ids=formula_ids,
            entities=[],
            cell_texts=cell_texts,
            table_bboxes=table_bboxes,
            cell_bboxes=cell_bboxes,
            table_bbox_by_id=table_bbox_by_id,
        )
        validator = SemanticTextValidator(
            gold_dir=gold_dir,
            paragraph_evidence=paragraph_evidence,
            table_cell_texts=cell_texts,
        )
        page_review = 0
        page_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            issues = validator.validate_chunk(chunk)
            if issues:
                chunk["review_status"] = "review_required"
                chunk["validation_issues"] = issues
                page_review += 1
                review_items.append(
                    {
                        "page_number": page_number,
                        "item_type": "semantic_text",
                        "chunk_id": chunk.get("chunk_id"),
                        "issues": issues,
                    }
                )
            page_chunks.append(chunk)
        all_chunks.extend(page_chunks)

        page_stats.append(
            {
                "page_number": page_number,
                "printed_page_number": page.printed_page_number,
                "chunks": len(chunks),
                "review_required": page_review,
                "tables_on_page": len(table_ids),
                "formulas_on_page": len(formula_ids),
            }
        )
        logger.info(
            "semantic_text_page_complete",
            page_number=page_number,
            chunks=len(chunks),
            review=page_review,
        )

    rag_dir = gold_dir / "rag"
    rag_dir.mkdir(parents=True, exist_ok=True)
    out_path = rag_dir / "semantic_text.jsonl"
    existing: list[dict[str, Any]] = []
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                existing.append(json.loads(line))
    merged = _merge_jsonl_records(existing, all_chunks, start_page=args.start_page, end_page=args.end_page)
    with out_path.open("w", encoding="utf-8") as handle:
        for record in merged:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    corpus_validation = SemanticTextValidator.validate_corpus(all_chunks)
    report_json, report_md = write_semantic_text_report(
        start_page=args.start_page,
        end_page=args.end_page,
        chunks=all_chunks,
        page_stats=page_stats,
        corpus_validation=corpus_validation,
        review_items=review_items,
        output_dir=PROJECT_ROOT / "docs" / "reports",
    )

    print("Semantic text pipeline complete:")
    print(f"  chunks_written: {len(all_chunks)}")
    print(f"  corpus_total: {len(merged)}")
    print(f"  review_items: {len(review_items)}")
    print(f"  output: {out_path}")
    print(f"  report_json: {report_json}")
    print(f"  report_md: {report_md}")


if __name__ == "__main__":
    main()
