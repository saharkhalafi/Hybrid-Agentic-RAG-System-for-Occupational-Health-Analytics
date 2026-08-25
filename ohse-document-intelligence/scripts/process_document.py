"""Phase 1 document processing — Layer 1 evidence + Layer 2 structural validation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.structural_resolver import resolve_structure, write_validated_structure

logger = get_logger(__name__)

OEL_PHYSICAL_COLUMNS = 7
OEL_COLUMN_HEADERS = ("TWA", "STEL", "STEL/C", "C")


def _parse_pages_argument(pages: str | int | None) -> tuple[int, int]:
    if pages is None:
        raise ValueError("--pages is required (e.g. 46-55)")
    if isinstance(pages, int):
        return 1, pages
    value = str(pages).strip()
    if re.fullmatch(r"\d+\-\d+", value):
        start, end = value.split("-", 1)
        return int(start), int(end)
    if value.isdigit():
        page = int(value)
        return page, page
    raise ValueError(f"Invalid --pages value: {pages}")


def _table_header_text(table: dict) -> str:
    parts: list[str] = []
    for row in table.get("rows") or []:
        for cell in row:
            text = str(cell.get("text") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def _validate_output(path: Path, *, start_page: int, end_page: int) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tables = payload.get("tables") or []
    cells = payload.get("cells") or []

    page_results: dict[int, dict] = {}
    for page_num in range(start_page, end_page + 1):
        page_tables = [t for t in tables if t.get("page_number") == page_num]
        page_cells = [c for c in cells if c.get("page_number") == page_num]
        oel_tables = [t for t in page_tables if t.get("table_type") == "chemical_oel"]

        checks: dict[str, bool | int | float | str] = {
            "tables_found": len(page_tables) > 0,
            "oel_tables": len(oel_tables),
        }

        if oel_tables:
            max_cols = max(len(row) for t in oel_tables for row in (t.get("rows") or []))
            header_blob = " ".join(_table_header_text(t) for t in oel_tables)
            checks["oel_physical_columns"] = max_cols
            checks["oel_seven_columns"] = max_cols == OEL_PHYSICAL_COLUMNS
            checks["twa_present"] = "TWA" in header_blob
            checks["stel_present"] = any(h in header_blob for h in OEL_COLUMN_HEADERS if h != "TWA")

        non_empty = [c for c in page_cells if str(c.get("text") or "").strip()]
        with_bbox = [c for c in non_empty if c.get("bbox")]
        bbox_conf = [
            float(c["bbox_confidence"])
            for c in non_empty
            if c.get("bbox_confidence") is not None
        ]
        checks["non_empty_cells"] = len(non_empty)
        checks["bbox_coverage_non_empty"] = (
            round(len(with_bbox) / len(non_empty), 4) if non_empty else 1.0
        )
        checks["avg_bbox_confidence"] = round(sum(bbox_conf) / len(bbox_conf), 4) if bbox_conf else 0.0

        failed = []
        if not checks["tables_found"]:
            failed.append("no_tables")
        if oel_tables:
            if not checks.get("oel_seven_columns"):
                failed.append("oel_column_count")
            if not checks.get("twa_present"):
                failed.append("missing_twa")
            if not checks.get("stel_present"):
                failed.append("missing_stel")

        checks["status"] = "PASS" if not failed else "FAIL"
        checks["failed_checks"] = failed
        page_results[page_num] = checks

    overall_fail = any(r["status"] == "FAIL" for r in page_results.values())
    return {
        "validated_structure": str(path),
        "pages_processed": end_page - start_page + 1,
        "document_ai_table_count": payload.get("document_ai_table_count"),
        "recovered_table_count": payload.get("recovered_table_count"),
        "logical_row_reconstruction_count": payload.get("logical_row_reconstruction_count"),
        "merged_cell_count": payload.get("merged_cell_count"),
        "geometry_aligned_cell_count": payload.get("geometry_aligned_cell_count"),
        "overall_status": "PASS" if not overall_fail else "FAIL",
        "pages": page_results,
    }


def process_document(
    file_path: Path,
    *,
    start_page: int,
    end_page: int,
    skip_document_ai: bool = False,
) -> Path:
    processor = DocumentProcessor()
    evidence = processor.process(
        file_path,
        start_page=start_page,
        end_page=end_page,
        skip_document_ai=skip_document_ai,
    )
    structural = resolve_structure(evidence, file_path)
    return write_validated_structure(
        structural,
        evidence,
        start_page=start_page,
        end_page=end_page,
    )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Process OHSE PDF pages — Layer 1 evidence + Layer 2 structural validation",
    )
    parser.add_argument("--file", required=True, type=Path, help="Path to PDF file")
    parser.add_argument(
        "--pages",
        required=True,
        help="Inclusive page range (e.g. 46-55)",
    )
    parser.add_argument(
        "--skip-document-ai",
        action="store_true",
        help="Use cached Document AI raw JSON when available",
    )
    parser.add_argument(
        "--validate-only",
        type=Path,
        default=None,
        help="Validate an existing validated_structure JSON (skip processing)",
    )
    args = parser.parse_args()

    start_page, end_page = _parse_pages_argument(args.pages)

    if args.validate_only:
        report = _validate_output(args.validate_only, start_page=start_page, end_page=end_page)
    else:
        pdf_path = args.file.resolve()
        if not pdf_path.exists():
            raise FileNotFoundError(pdf_path)
        output_path = process_document(
            pdf_path,
            start_page=start_page,
            end_page=end_page,
            skip_document_ai=args.skip_document_ai,
        )
        logger.info("phase1_complete", output=str(output_path))
        report = _validate_output(output_path, start_page=start_page, end_page=end_page)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["overall_status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
