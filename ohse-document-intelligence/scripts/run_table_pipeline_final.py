"""Final production table Goldset pipeline — all 59 OEL table pages.

Orchestrates (no architecture changes):
  1. Validated v6 table extraction + gate
  2. Gold promotion (gold_allowed only)
  3. Full gold artifact sync via GoldsetPipeline
  4. RAG corpus build
  5. PostgreSQL persistence
  6. HITL bridge refresh
  7. FINAL reports
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fitz

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from goldset_generator.pipeline import GoldsetPipeline
from goldset_generator.review_manager import ReviewManager
from scripts.run_table_pipeline_all_pages import (
    DEFAULT_BATCHES,
    aggregate_summary,
    build_inventory,
    discover_table_pages,
    process_batch,
    worst_tables,
    write_markdown_report,
)

logger = get_logger(__name__)

TABLE_PAGE_RANGES = DEFAULT_BATCHES


def _promote_table_gold(
    *,
    table_id: str,
    candidate: dict[str, Any],
    review_manager: ReviewManager,
) -> str:
    """Promote to gold/tables only when gate passes; otherwise candidates only."""
    settings = get_settings()
    candidate_dir = settings.gold_dir / "candidates" / "tables"
    gold_dir = settings.gold_dir / "tables"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(candidate, ensure_ascii=False, indent=2)
    (candidate_dir / f"{table_id}.json").write_text(payload, encoding="utf-8")

    if candidate.get("gold_allowed"):
        gold_dir.mkdir(parents=True, exist_ok=True)
        (gold_dir / f"{table_id}.json").write_text(payload, encoding="utf-8")
        return "gold"
    gold_path = gold_dir / f"{table_id}.json"
    if gold_path.exists():
        gold_path.unlink()
    return "review_required"


def _confidence_bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    if conf >= 0.95:
        return "0.95+"
    if conf >= 0.90:
        return "0.90-0.94"
    if conf >= 0.85:
        return "0.85-0.89"
    if conf >= 0.80:
        return "0.80-0.84"
    return "<0.80"


def _run_subprocess(cmd: list[str], *, label: str) -> dict[str, Any]:
    logger.info("subprocess_start", label=label, cmd=" ".join(cmd))
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("subprocess_failed", label=label, stderr=result.stderr[-2000:])
    return {
        "label": label,
        "exit_code": result.returncode,
        "stdout_tail": (result.stdout or "")[-1500:],
        "stderr_tail": (result.stderr or "")[-1500:],
    }


def _sync_gold_artifacts(
    pdf_path: Path,
    *,
    batches: list[tuple[int, int]],
    skip_document_ai: bool,
    force: bool,
) -> list[dict[str, Any]]:
    pipeline = GoldsetPipeline()
    results: list[dict[str, Any]] = []
    for start, end in batches:
        stats = pipeline.run(
            pdf_path,
            start_page=start,
            end_page=end,
            force=force,
            skip_document_ai=skip_document_ai,
        )
        results.append({"batch": f"{start}-{end}", **stats})
    return results


def _reconcile_gold_gate(diagnostics: list[dict[str, Any]]) -> dict[str, int]:
    """Ensure gold/tables only contains gold_allowed tables after full pipeline."""
    review_manager = ReviewManager(get_settings().gold_dir)
    counts = {"gold": 0, "review_required": 0, "quarantined": 0}
    by_id = {d["table_id"]: d for d in diagnostics}
    settings = get_settings()
    gold_dir = settings.gold_dir / "tables"
    candidate_dir = settings.gold_dir / "candidates" / "tables"

    for path in gold_dir.glob("table_*.json"):
        table_id = path.stem
        diag = by_id.get(table_id)
        if diag and not diag.get("gold_allowed"):
            candidate = json.loads(path.read_text(encoding="utf-8"))
            status = _promote_table_gold(
                table_id=table_id, candidate=candidate, review_manager=review_manager
            )
            counts[status] = counts.get(status, 0) + 1
        elif diag and diag.get("gold_allowed"):
            counts["gold"] += 1

    for table_id, diag in by_id.items():
        cand_path = candidate_dir / f"{table_id}.json"
        if not cand_path.exists():
            continue
        candidate = json.loads(cand_path.read_text(encoding="utf-8"))
        status = _promote_table_gold(
            table_id=table_id, candidate=candidate, review_manager=review_manager
        )
        if status == "gold":
            counts["gold"] += 0  # already counted
        elif status == "review_required":
            counts["review_required"] += 1

    return counts


def _build_final_table_rows(diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in sorted(diagnostics, key=lambda x: x.get("page_number", 0)):
        tq = d.get("table_quality") or {}
        struct_ok = all(
            tq.get(k, True)
            for k in (
                "geometry_valid",
                "column_count_valid",
                "header_structure_valid",
                "merged_cells_valid",
            )
        )
        gold = bool(d.get("gold_allowed"))
        rows.append(
            {
                "page": d.get("page_number"),
                "table_id": d.get("table_id"),
                "family": d.get("table_family"),
                "schema_version": d.get("schema_id"),
                "rows": d.get("physical_row_count"),
                "columns": d.get("physical_column_count"),
                "extraction_method": d.get("recovery_method") or "document_ai",
                "structural_status": "PASS" if struct_ok else "FAIL",
                "numeric_status": "PASS" if tq.get("numeric_integrity_valid") else "FAIL",
                "provenance_status": "PASS" if tq.get("provenance_complete") else "FAIL",
                "gold_allowed": gold,
                "final_status": "GOLD" if gold else "REVIEW_REQUIRED",
            }
        )
    return rows


def write_final_report(
    path_json: Path,
    path_md: Path,
    *,
    summary: dict[str, Any],
    inventory: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    table_rows: list[dict[str, Any]],
    gold_counts: dict[str, int],
    hitl_stats: dict[str, Any],
    subprocess_log: list[dict[str, Any]],
    test_result: dict[str, Any],
) -> None:
    conf_dist = Counter(_confidence_bucket(d.get("structural_confidence")) for d in diagnostics)
    extraction_methods = Counter(
        d.get("recovery_method") or "document_ai" for d in diagnostics
    )

    payload = {
        "summary": {
            **summary,
            "gold_tables": gold_counts.get("gold", 0),
            "review_required_tables": gold_counts.get("review_required", 0),
            "quarantined_tables": gold_counts.get("quarantined", 0),
            "confidence_distribution": dict(conf_dist),
            "extraction_methods": dict(extraction_methods),
            "hitl": hitl_stats,
            "subprocess_log": subprocess_log,
            "test_result": test_result,
        },
        "inventory": inventory,
        "per_table": table_rows,
        "tables": diagnostics,
    }
    path_json.parent.mkdir(parents=True, exist_ok=True)
    path_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Table Pipeline — FINAL",
        "",
        f"Generated: {summary.get('generated_at')}",
        f"PDF: `{summary.get('source_pdf')}`",
        "",
        "## Final statistics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for key in (
        "total_pages",
        "table_pages",
        "total_tables",
        "gold_tables",
        "review_required_tables",
        "quarantined_tables",
        "gold_allowed_rate",
        "seven_column_reconstruction_rate",
        "mega_cell_count",
        "numeric_integrity_rate",
        "provenance_coverage",
        "document_ai_recovery_rate",
    ):
        val = summary.get(key) if key in summary else payload["summary"].get(key)
        lines.append(f"| {key} | {val} |")

    lines.extend(["", "## Confidence distribution", ""])
    for bucket, count in sorted(conf_dist.items()):
        lines.append(f"- {bucket}: {count}")

    lines.extend(["", "## Extraction methods", ""])
    for method, count in extraction_methods.items():
        lines.append(f"- {method}: {count}")

    lines.extend(["", "## Per-table summary", ""])
    lines.append(
        "| page | table_id | family | schema | rows | cols | method | structural | numeric | provenance | gold_allowed | final_status |"
    )
    lines.append(
        "|------|----------|--------|--------|------|------|--------|------------|---------|------------|--------------|--------------|"
    )
    for row in table_rows:
        lines.append(
            f"| {row['page']} | {row['table_id']} | {row['family']} | {row['schema_version']} | "
            f"{row['rows']} | {row['columns']} | {row['extraction_method']} | "
            f"{row['structural_status']} | {row['numeric_status']} | {row['provenance_status']} | "
            f"{row['gold_allowed']} | {row['final_status']} |"
        )

    lines.extend(["", "## HITL", ""])
    for k, v in hitl_stats.items():
        lines.append(f"- {k}: {v}")

    lines.extend(["", "## Tests", ""])
    lines.append(f"- exit_code: {test_result.get('exit_code')}")
    lines.append(f"- passed: {test_result.get('passed')}")

    path_md.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Final table Goldset production run")
    parser.add_argument("--file", type=Path, default=PROJECT_ROOT.parent / "OHE6.pdf")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages_FINAL.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages_FINAL.md",
    )
    parser.add_argument("--skip-document-ai", action="store_true", default=True)
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--skip-hitl", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"PDF not found: {args.file}")

    settings = get_settings()
    doc = fitz.open(args.file)
    total_pdf_pages = doc.page_count
    doc.close()

    table_pages = discover_table_pages(args.file)
    logger.info("final_run_start", table_pages=len(table_pages))

    # Stage 1: validated table extraction + candidates
    all_diagnostics: list[dict[str, Any]] = []
    review_manager = ReviewManager(settings.gold_dir)
    promote_counts = {"gold": 0, "review_required": 0, "quarantined": 0}

    for start, end in TABLE_PAGE_RANGES:
        batch_pages = [p for p in table_pages if start <= p <= end]
        if not batch_pages:
            continue
        batch_diags = process_batch(
            args.file,
            start_page=start,
            end_page=end,
            skip_document_ai=args.skip_document_ai,
        )
        for diag in batch_diags:
            table_id = diag.get("table_id", "")
            cand_path = settings.gold_dir / "candidates" / "tables" / f"{table_id}.json"
            if cand_path.exists():
                candidate = json.loads(cand_path.read_text(encoding="utf-8"))
                status = _promote_table_gold(
                    table_id=table_id,
                    candidate=candidate,
                    review_manager=review_manager,
                )
                promote_counts[status] = promote_counts.get(status, 0) + 1
        all_diagnostics.extend(batch_diags)

    by_id: dict[str, dict[str, Any]] = {}
    for d in all_diagnostics:
        by_id[d.get("table_id", "")] = d
    all_diagnostics = sorted(by_id.values(), key=lambda x: x.get("page_number", 0))

    # Stage 2: full gold artifact sync (pages, entities, formulas, rag, review)
    artifact_results = _sync_gold_artifacts(
        args.file,
        batches=TABLE_PAGE_RANGES,
        skip_document_ai=args.skip_document_ai,
        force=args.force,
    )

    # Stage 3: reconcile gold gate after full pipeline
    gate_counts = _reconcile_gold_gate(all_diagnostics)
    promote_counts.update({k: max(promote_counts.get(k, 0), gate_counts.get(k, 0)) for k in gate_counts})

    subprocess_log: list[dict[str, Any]] = []

    # Stage 4: RAG corpus
    subprocess_log.append(
        _run_subprocess(
            [
                sys.executable,
                "scripts/build_rag_corpus.py",
                "--start-page",
                "46",
                "--end-page",
                "144",
            ],
            label="build_rag_corpus",
        )
    )

    # Stage 5: PostgreSQL
    if not args.skip_postgres:
        for start, end in TABLE_PAGE_RANGES:
            subprocess_log.append(
                _run_subprocess(
                    [
                        sys.executable,
                        "scripts/persist_evidence_pipeline.py",
                        "--file",
                        str(args.file),
                        "--start-page",
                        str(start),
                        "--end-page",
                        str(end),
                    ],
                    label=f"persist_postgres_{start}-{end}",
                )
            )
        subprocess_log.append(
            _run_subprocess([sys.executable, "scripts/load_rag_to_postgres.py"], label="load_rag_postgres")
        )

    # Stage 6: HITL bridge
    hitl_stats: dict[str, Any] = {"skipped": args.skip_hitl}
    if not args.skip_hitl:
        pages_arg = ",".join(str(p) for p in table_pages)
        hitl_result = _run_subprocess(
            [
                sys.executable,
                "scripts/bridge_hitl_review.py",
                "--pdf-name",
                args.file.name,
                "--pages",
                pages_arg,
                "--import-queue",
            ],
            label="bridge_hitl",
        )
        subprocess_log.append(hitl_result)
        try:
            hitl_stats = json.loads(hitl_result.get("stdout_tail", "") or "{}")
        except json.JSONDecodeError:
            hitl_stats = {"bridge_exit_code": hitl_result.get("exit_code")}

    # Stage 7: tests
    test_result: dict[str, Any] = {"skipped": args.skip_tests}
    if not args.skip_tests:
        tr = _run_subprocess([sys.executable, "-m", "pytest", "-q"], label="pytest")
        subprocess_log.append(tr)
        stdout = tr.get("stdout_tail", "")
        passed = None
        if " passed" in stdout:
            import re

            m = re.search(r"(\d+) passed", stdout)
            if m:
                passed = int(m.group(1))
        test_result = {"exit_code": tr.get("exit_code"), "passed": passed}

    inventory = build_inventory(all_diagnostics, table_pages)
    summary = aggregate_summary(
        pdf_path=args.file,
        total_pdf_pages=total_pdf_pages,
        table_pages=table_pages,
        diagnostics=all_diagnostics,
    )
    summary["gold_tables"] = promote_counts.get("gold", 0)
    summary["review_required_tables"] = promote_counts.get("review_required", 0)
    summary["quarantined_tables"] = promote_counts.get("quarantined", 0)
    summary["promote_gold"] = True
    summary["artifact_batches"] = artifact_results

    table_rows = _build_final_table_rows(all_diagnostics)
    write_final_report(
        args.output_json,
        args.output_md,
        summary=summary,
        inventory=inventory,
        diagnostics=all_diagnostics,
        table_rows=table_rows,
        gold_counts=promote_counts,
        hitl_stats=hitl_stats,
        subprocess_log=subprocess_log,
        test_result=test_result,
    )

    # Also refresh non-FINAL report for continuity
    write_markdown_report(
        PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages.md",
        summary=summary,
        inventory=inventory,
        diagnostics=all_diagnostics,
        worst=worst_tables(all_diagnostics, limit=20),
    )
    (PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "inventory": inventory,
                "tables": all_diagnostics,
                "worst_review_required": worst_tables(all_diagnostics, limit=20),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
