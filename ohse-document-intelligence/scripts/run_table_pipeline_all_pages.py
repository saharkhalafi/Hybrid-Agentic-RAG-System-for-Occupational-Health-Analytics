"""Run validated v6 table pipeline across all table-containing PDF pages.

Uses table diagnostics from `scripts/table_diagnose.py`:
  Document AI evidence → structural resolver → canonical grid → gold candidates

Does NOT promote to gold/tables or refresh HITL.
run_table_pipeline_all_pages.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fitz

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.structural_resolver import resolve_structure, write_validated_structure
from goldset_generator.table_detection_gate import has_chemical_oel_signatures
from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator
from pipeline_contracts.canonical_grid import CanonicalGridBuilder
from pipeline_contracts.evidence import EvidenceStore
from pipeline_contracts.header_reconstruction import reconstruct_header_structure
from pipeline_contracts.table_family_classifier import TableFamilyClassifier
from scripts.table_diagnose import diagnose_table

logger = get_logger(__name__)

# Batches covering all 59 OEL table pages (46–110, 140, 144).
DEFAULT_BATCHES: list[tuple[int, int]] = [
    (46, 70),
    (71, 95),
    (96, 110),
    (140, 140),
    (144, 144),
]


def discover_table_pages(pdf_path: Path) -> list[int]:
    """Inventory pages containing tables via OEL signatures + DA hints."""
    doc = fitz.open(pdf_path)
    pages: set[int] = set()
    try:
        for idx in range(doc.page_count):
            text = doc.load_page(idx).get_text()
            if has_chemical_oel_signatures(text):
                pages.add(idx + 1)
    finally:
        doc.close()
    return sorted(pages)


def _document_ai_column_count(table: dict[str, Any]) -> int | None:
    rows = table.get("rows") or []
    if not rows:
        return None
    cols: set[int] = set()
    for row in rows:
        for cell in row:
            if isinstance(cell, dict):
                cols.add(int(cell.get("column") or 0))
    return len(cols) if cols else None


def _mega_cell_count(cells: list[dict[str, Any]]) -> int:
    count = 0
    for cell in cells:
        if cell.get("row", 0) == 0:
            continue
        bbox = cell.get("bbox") or {}
        if bbox.get("width", 0) > 200:
            count += 1
    return count


def _review_reason(diag: dict[str, Any]) -> str:
    if diag.get("gold_allowed"):
        return ""
    reasons: list[str] = []
    tq = diag.get("table_quality") or {}
    if not tq.get("geometry_valid", True):
        reasons.append("geometry")
    if not tq.get("header_structure_valid", True):
        reasons.append("header_reconstruction")
    if not tq.get("merged_cells_valid", True):
        reasons.append("merged_cells")
    if not tq.get("numeric_integrity_valid", True):
        reasons.append("numeric_integrity")
    if not tq.get("provenance_complete", True):
        reasons.append("provenance")
    if diag.get("table_family") in {"unknown", "regulation", "other"}:
        reasons.append("unknown_table_family")
    for issue in diag.get("validation_issues") or []:
        if issue not in reasons:
            reasons.append(issue)
    return ", ".join(reasons) or "review_required"


def _failure_clusters(diag: dict[str, Any]) -> list[str]:
    if diag.get("gold_allowed"):
        return []
    clusters: list[str] = []
    tq = diag.get("table_quality") or {}
    issues = tq.get("issues") or []
    issue_types = {i.get("type") for i in issues if isinstance(i, dict)}

    if not tq.get("header_structure_valid", True):
        clusters.append("header_reconstruction")
    if diag.get("physical_column_count") != 7 and diag.get("table_family") == "chemical_oel":
        clusters.append("column_boundary")
    if any(i.get("type") == "ROW_NUMBER_MULTI_VALUE" for i in issues):
        clusters.append("row_segmentation")
    if "merged_cell_unresolved" in issue_types:
        clusters.append("merged_cells")
    if diag.get("numeric_validation"):
        clusters.append("numeric_integrity")
    if any("CAS" in str(i.get("field", "")) for i in diag.get("numeric_validation") or []):
        clusters.append("CAS/entity_extraction")
    if diag.get("table_family") in {"unknown"}:
        clusters.append("unknown_table_family")
    if not tq.get("provenance_complete", True):
        clusters.append("provenance")
    if _mega_cell_count(diag.get("reconstructed_cells") or []) > 0:
        clusters.append("column_boundary")
    if not clusters:
        clusters.append("other")
    return sorted(set(clusters))


def _cached_raw_path(start: int, end: int) -> Path | None:
    settings = get_settings()
    exact = settings.data_intermediate_dir / f"document_ai_raw_{start}-{end}.json"
    if exact.exists():
        return exact
    # Overlapping caches
    for candidate in (
        settings.data_intermediate_dir / f"document_ai_raw_{start}-{end}.json",
        settings.data_intermediate_dir / "document_ai_raw_46-70.json"
        if start >= 46 and end <= 70
        else None,
        settings.data_intermediate_dir / "document_ai_raw_46-55.json"
        if start >= 46 and end <= 55
        else None,
    ):
        if candidate and candidate.exists():
            return candidate
    return None


def process_batch(
    pdf_path: Path,
    *,
    start_page: int,
    end_page: int,
    skip_document_ai: bool = False,
) -> list[dict[str, Any]]:
    settings = get_settings()
    processor = DocumentProcessor()
    cached = _cached_raw_path(start_page, end_page)
    logger.info(
        "batch_start",
        pages=f"{start_page}-{end_page}",
        cached=str(cached) if cached else None,
        skip_document_ai=skip_document_ai and cached is None,
    )

    evidence = processor.process(
        pdf_path,
        start_page=start_page,
        end_page=end_page,
        skip_document_ai=skip_document_ai and cached is None,
        cached_raw_json=cached,
    )

    manifest = EvidenceStore().persist(
        evidence,
        pipeline_version=settings.goldset_pipeline_version,
        processor_version=settings.processing_version,
        force=True,
    )

    structural = resolve_structure(evidence, pdf_path)
    write_validated_structure(
        structural,
        evidence,
        start_page=start_page,
        end_page=end_page,
    )

    grid_builder = CanonicalGridBuilder(
        pipeline_version=settings.goldset_pipeline_version,
        processor_version=settings.processing_version,
        evidence_refs=list(manifest.artifacts.values()),
    )
    structural_tables = [t.to_dict() for t in structural.tables]
    canonical_grids = grid_builder.build_all(structural_tables)
    CanonicalGridBuilder.write_all(
        canonical_grids,
        content_hash=evidence.content_hash,
        start_page=start_page,
        end_page=end_page,
    )

    candidate_dir = settings.gold_dir / "candidates" / "tables"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    generator = TableGoldGenerator()
    classifier = TableFamilyClassifier()
    page_detection = structural.page_detection

    diagnostics: list[dict[str, Any]] = []
    for table in structural_tables:
        page_num = int(table.get("page_number") or 0)
        det = page_detection.get(page_num, {})
        recovery_method = det.get("recovery_method")
        if det.get("table_detection_status") == "recovered":
            recovery_method = recovery_method or "pymupdf_word_grid"

        family = classifier.classify_table_dict(table)
        diag = diagnose_table(table, recovery_method=recovery_method)
        all_cells = [c for row in table.get("rows") or [] for c in row if isinstance(c, dict)]

        diag.update(
            {
                "table_family": family.table_type,
                "schema_id": family.schema_id,
                "family_confidence": family.confidence,
                "family_classifier": family.classifier,
                "document_ai_column_count": _document_ai_column_count(table),
                "document_ai_table_count": 1 if det.get("table_detection_status") == "detected" else 0,
                "table_detection_status": det.get("table_detection_status"),
                "mega_cell_count": _mega_cell_count(diag.get("reconstructed_cells") or []),
                "review_reason": _review_reason(diag),
                "failure_clusters": _failure_clusters(diag),
            }
        )
        diagnostics.append(diag)

        candidate = generator.generate(table)
        candidate["table_family"] = family.to_dict()
        tid = table.get("table_id", "")
        (candidate_dir / f"{tid}.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return diagnostics


def build_inventory(diagnostics: list[dict[str, Any]], table_pages: list[int]) -> list[dict[str, Any]]:
    by_page = {d["page_number"]: d for d in diagnostics}
    inventory: list[dict[str, Any]] = []
    for page in table_pages:
        d = by_page.get(page)
        if d:
            inventory.append(
                {
                    "page": page,
                    "table_id": d.get("table_id"),
                    "table_family": d.get("table_family"),
                    "document_ai_column_count": d.get("document_ai_column_count"),
                    "recovery_method": d.get("recovery_method") or "document_ai",
                    "physical_column_count": d.get("physical_column_count"),
                }
            )
        else:
            inventory.append(
                {
                    "page": page,
                    "table_id": None,
                    "table_family": "missing",
                    "document_ai_column_count": None,
                    "recovery_method": None,
                    "physical_column_count": None,
                }
            )
    return inventory


def aggregate_summary(
    *,
    pdf_path: Path,
    total_pdf_pages: int,
    table_pages: list[int],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for d in diagnostics if d.get("gold_allowed"))
    review = sum(1 for d in diagnostics if d.get("review_required"))
    failed = len(diagnostics) - passed
    recovered = sum(1 for d in diagnostics if d.get("recovery_method") == "pymupdf_word_grid")
    seven_col = sum(1 for d in diagnostics if d.get("physical_column_count") == 7)
    mega_total = sum(d.get("mega_cell_count", 0) for d in diagnostics)
    numeric_ok = sum(1 for d in diagnostics if (d.get("table_quality") or {}).get("numeric_integrity_valid"))
    prov_ok = sum(1 for d in diagnostics if (d.get("table_quality") or {}).get("provenance_complete"))

    cluster_counter: Counter[str] = Counter()
    for d in diagnostics:
        for c in d.get("failure_clusters") or []:
            cluster_counter[c] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_pdf": str(pdf_path.resolve()),
        "total_pages": total_pdf_pages,
        "table_pages": len(table_pages),
        "total_tables": len(diagnostics),
        "tables_passed": passed,
        "tables_review_required": review,
        "tables_failed": failed,
        "gold_allowed_rate": round(passed / len(diagnostics), 4) if diagnostics else 0.0,
        "review_required_rate": round(review / len(diagnostics), 4) if diagnostics else 0.0,
        "document_ai_recovery_rate": round(
            sum(1 for d in diagnostics if d.get("table_detection_status") == "recovered") / len(diagnostics),
            4,
        )
        if diagnostics
        else 0.0,
        "seven_column_reconstruction_rate": round(seven_col / len(diagnostics), 4) if diagnostics else 0.0,
        "mega_cell_count": mega_total,
        "numeric_integrity_rate": round(numeric_ok / len(diagnostics), 4) if diagnostics else 0.0,
        "provenance_coverage": round(prov_ok / len(diagnostics), 4) if diagnostics else 0.0,
        "failure_cluster_counts": dict(cluster_counter),
        "promote_gold": False,
        "hitl_refreshed": False,
    }


def worst_tables(diagnostics: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    def severity(d: dict[str, Any]) -> tuple:
        tq = d.get("table_quality") or {}
        return (
            0 if d.get("gold_allowed") else 1,
            len(d.get("numeric_validation") or []),
            d.get("mega_cell_count", 0),
            0 if tq.get("provenance_complete") else 1,
        )

    ranked = sorted(diagnostics, key=severity, reverse=True)
    worst: list[dict[str, Any]] = []
    for d in ranked[:limit]:
        if d.get("gold_allowed"):
            continue
        worst.append(
            {
                "page": d.get("page_number"),
                "table_id": d.get("table_id"),
                "family": d.get("table_family"),
                "review_reason": d.get("review_reason"),
                "failure_clusters": d.get("failure_clusters"),
                "mega_cells": d.get("mega_cell_count"),
                "numeric_issues": len(d.get("numeric_validation") or []),
                "structural_confidence": d.get("structural_confidence"),
            }
        )
    return worst[:limit]


def write_markdown_report(
    path: Path,
    *,
    summary: dict[str, Any],
    inventory: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    worst: list[dict[str, Any]],
) -> None:
    lines = [
        "# Table Pipeline — All Pages",
        "",
        f"Generated: {summary['generated_at']}",
        f"PDF: `{summary['source_pdf']}`",
        "",
        "## Global totals",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for key in (
        "total_pages",
        "table_pages",
        "total_tables",
        "tables_passed",
        "tables_review_required",
        "tables_failed",
        "gold_allowed_rate",
        "review_required_rate",
        "document_ai_recovery_rate",
        "seven_column_reconstruction_rate",
        "mega_cell_count",
        "numeric_integrity_rate",
        "provenance_coverage",
    ):
        lines.append(f"| {key} | {summary.get(key)} |")

    lines.extend(["", "## Per-table results", ""])
    lines.append(
        "| page | table_id | family | cols | rows | recovery | structural | numeric | provenance | gold_allowed | review_reason |"
    )
    lines.append(
        "|------|----------|--------|------|------|----------|------------|---------|------------|--------------|---------------|"
    )
    for d in sorted(diagnostics, key=lambda x: x.get("page_number", 0)):
        tq = d.get("table_quality") or {}
        struct_ok = "PASS" if all(
            tq.get(k) for k in ("geometry_valid", "column_count_valid", "header_structure_valid", "merged_cells_valid")
        ) else "FAIL"
        lines.append(
            f"| {d.get('page_number')} | {d.get('table_id')} | {d.get('table_family')} | "
            f"{d.get('physical_column_count')} | {d.get('physical_row_count')} | "
            f"{d.get('recovery_method') or 'document_ai'} | {struct_ok} | "
            f"{'PASS' if tq.get('numeric_integrity_valid') else 'FAIL'} | "
            f"{'PASS' if tq.get('provenance_complete') else 'FAIL'} | "
            f"{d.get('gold_allowed')} | {d.get('review_reason') or '-'} |"
        )

    lines.extend(["", "## Failure clusters", ""])
    for cluster, count in sorted((summary.get("failure_cluster_counts") or {}).items(), key=lambda x: -x[1]):
        lines.append(f"- **{cluster}**: {count}")

    lines.extend(["", "## Top review-required tables", ""])
    for item in worst:
        lines.append(
            f"- Page {item['page']} `{item['table_id']}` ({item['family']}): "
            f"{item['review_reason']} — clusters: {', '.join(item.get('failure_clusters') or [])}"
        )

    lines.extend(["", "## Page inventory", ""])
    for item in inventory:
        lines.append(
            f"- p{item['page']}: `{item.get('table_id')}` family={item.get('table_family')} "
            f"DA_cols={item.get('document_ai_column_count')} physical_cols={item.get('physical_column_count')} "
            f"recovery={item.get('recovery_method')}"
        )

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run table pipeline on all table pages")
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT.parent / "OHE6.pdf")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages.md",
    )
    parser.add_argument(
        "--skip-document-ai",
        action="store_true",
        help="Skip Document AI API when no cached raw JSON exists (PyMuPDF recovery only)",
    )
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"PDF not found: {args.file}")

    doc = fitz.open(args.file)
    total_pdf_pages = doc.page_count
    doc.close()

    table_pages = discover_table_pages(args.file)
    logger.info("discovered_table_pages", count=len(table_pages), pages=f"{table_pages[0]}-{table_pages[-1]}")

    all_diagnostics: list[dict[str, Any]] = []
    for start, end in DEFAULT_BATCHES:
        batch_pages = [p for p in table_pages if start <= p <= end]
        if not batch_pages:
            continue
        batch_diags = process_batch(
            args.file,
            start_page=start,
            end_page=end,
            skip_document_ai=args.skip_document_ai,
        )
        all_diagnostics.extend(batch_diags)

    # Deduplicate by table_id (46-55 processed in both 46-70 and dedicated v6 — keep latest)
    by_id: dict[str, dict[str, Any]] = {}
    for d in all_diagnostics:
        by_id[d.get("table_id", "")] = d
    all_diagnostics = sorted(by_id.values(), key=lambda x: x.get("page_number", 0))

    inventory = build_inventory(all_diagnostics, table_pages)
    summary = aggregate_summary(
        pdf_path=args.file,
        total_pdf_pages=total_pdf_pages,
        table_pages=table_pages,
        diagnostics=all_diagnostics,
    )
    worst = worst_tables(all_diagnostics, limit=20)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "inventory": inventory,
        "tables": all_diagnostics,
        "worst_review_required": worst,
    }
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(
        args.output_md,
        summary=summary,
        inventory=inventory,
        diagnostics=all_diagnostics,
        worst=worst,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nReport: {args.output_json}")
    print(f"Markdown: {args.output_md}")


if __name__ == "__main__":
    main()
