"""Run full OHSE pipeline (gold + semantic text + RAG) for a page range in batches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fitz

from config.settings import get_settings
from goldset_generator.review_manager import ReviewManager


def _run(cmd: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)


def _preflight(pdf_path: Path, start_page: int, end_page: int) -> dict:
    settings = get_settings()
    doc = fitz.open(pdf_path)
    total = doc.page_count
    checks = {
        "pdf_path": str(pdf_path),
        "pdf_total_pages": total,
        "requested_start": start_page,
        "requested_end": end_page,
        "page_range_valid": 1 <= start_page <= end_page <= total,
        "document_ai_processor": settings.document_ai_processor_name,
        "gemini_model": settings.llm_model,
        "gold_dir": str(settings.gold_dir),
        "approved_protection": True,
        "numerical_truth_source": "extracted/validated evidence cells and table gold",
        "gemini_scope": "semantic interpretation only (headers, formula semantics, ambiguous classification)",
    }
    doc.close()
    return checks


def _batch_ranges(start: int, end: int, batch_size: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    current = start
    while current <= end:
        batch_end = min(current + batch_size - 1, end)
        ranges.append((current, batch_end))
        current = batch_end + 1
    return ranges


def main() -> None:
    parser = argparse.ArgumentParser(description="Full pipeline for page range with semantic text")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--start-page", type=int, default=21)
    parser.add_argument("--end-page", type=int, default=387)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--skip-document-ai",
        action="store_true",
        help="Use PyMuPDF/recovery only (when Document AI cache/API unavailable)",
    )
    parser.add_argument("--skip-postgres", action="store_true")
    args = parser.parse_args()

    pdf_path = args.file.resolve()
    settings = get_settings()
    review = ReviewManager(settings.gold_dir)

    preflight = _preflight(pdf_path, args.start_page, args.end_page)
    print("Pre-flight checks:")
    for key, value in preflight.items():
        print(f"  {key}: {value}")
    if not preflight["page_range_valid"]:
        raise SystemExit("Invalid page range for PDF")

    protected = []
    for page in range(args.start_page, args.end_page + 1):
        existing = review.load_page_gold(page)
        if existing and existing.get("review_status") in {"approved", "corrected"}:
            protected.append(page)
    print(f"  protected_pages: {len(protected)}")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    aggregate = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "start_page": args.start_page,
        "end_page": args.end_page,
        "batches": [],
        "pages_processed": 0,
        "pages_skipped": 0,
        "pages_failed": 0,
        "tables_generated": 0,
        "formulas_generated": 0,
        "entities_generated": 0,
        "semantic_text_chunks": 0,
        "semantic_chunks_review": 0,
        "validation_failures": 0,
        "processing_errors": [],
        "approved_records_protected": len(protected),
    }

    for batch_start, batch_end in _batch_ranges(args.start_page, args.end_page, args.batch_size):
        cmd = [
            sys.executable,
            "scripts/generate_goldset.py",
            "--file",
            str(pdf_path),
            "--start-page",
            str(batch_start),
            "--end-page",
            str(batch_end),
        ]
        if args.force:
            cmd.append("--force")
        if args.skip_document_ai:
            cmd.append("--skip-document-ai")

        result = _run(cmd, env=env)
        batch_stats: dict = {"start": batch_start, "end": batch_end, "exit_code": result.returncode}
        if result.returncode != 0:
            aggregate["pages_failed"] += batch_end - batch_start + 1
            aggregate["processing_errors"].append(
                {"batch": f"{batch_start}-{batch_end}", "stage": "generate_goldset"}
            )
        aggregate["batches"].append(batch_stats)

    rag_result = _run(
        [
            sys.executable,
            "scripts/build_rag_corpus.py",
            "--start-page",
            str(args.start_page),
            "--end-page",
            str(args.end_page),
        ],
        env=env,
    )
    if rag_result.returncode != 0:
        aggregate["processing_errors"].append({"stage": "build_rag_corpus"})

    if not args.skip_postgres:
        for script in ("scripts/persist_evidence_pipeline.py", "scripts/load_rag_to_postgres.py"):
            if script.endswith("persist_evidence_pipeline.py"):
                cmd = [
                    sys.executable,
                    script,
                    "--file",
                    str(pdf_path),
                    "--start-page",
                    str(args.start_page),
                    "--end-page",
                    str(args.end_page),
                ]
            else:
                cmd = [sys.executable, script]
            pg_result = _run(cmd, env=env)
            if pg_result.returncode != 0:
                aggregate["processing_errors"].append({"stage": script})

    rag_dir = settings.gold_dir / "rag"
    semantic_path = rag_dir / "semantic_text.jsonl"
    semantic_count = 0
    if semantic_path.exists():
        semantic_count = sum(1 for line in semantic_path.read_text(encoding="utf-8").splitlines() if line.strip())

    tables_count = len(list((settings.gold_dir / "tables").glob("table_*.json")))
    formulas_count = len(list((settings.gold_dir / "formulas").glob("formula_*.json")))
    pages_count = len(list((settings.gold_dir / "pages").glob("page_*.json")))

    report_path = PROJECT_ROOT / "docs" / "reports" / "full_pipeline_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    aggregate.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "pages_in_gold_index": pages_count,
            "tables_in_gold": tables_count,
            "formulas_in_gold": formulas_count,
            "semantic_text_chunks_total": semantic_count,
            "semantic_text_output": str(semantic_path),
            "rag_dir": str(rag_dir),
            "structures_preserved": [
                "gold/pages",
                "gold/entities",
                "gold/formulas",
                "gold/tables",
                "gold/rag",
                "gold/review",
            ],
        }
    )
    report_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = PROJECT_ROOT / "docs" / "reports" / "full_pipeline_report.md"
    md_lines = [
        "# Full Pipeline Report",
        "",
        f"Range: **{args.start_page}–{args.end_page}**",
        f"Started: {aggregate['started_at']}",
        f"Finished: {aggregate['finished_at']}",
        "",
        "## Results",
        "",
        f"- Pages in gold index: {pages_count}",
        f"- Tables in gold: {tables_count}",
        f"- Formulas in gold: {formulas_count}",
        f"- Semantic text chunks (total in corpus): {semantic_count}",
        f"- Approved records protected: {len(protected)}",
        f"- Processing errors: {len(aggregate['processing_errors'])}",
        "",
        f"Semantic retrieval output: `{semantic_path}`",
        f"RAG directory: `{rag_dir}`",
        "",
        "## Traceability",
        "",
        "Semantic chunks preserve: PDF page → text_evidence_id → paragraph bbox → chunk_id",
        "",
        "## Structures preserved",
        "",
        "- gold/pages/, gold/entities/, gold/formulas/, gold/tables/, gold/rag/, gold/review/",
        "",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print("\nFull pipeline report:")
    print(f"  json: {report_path}")
    print(f"  markdown: {md_path}")
    print(f"  semantic_text_chunks_total: {semantic_count}")
    if aggregate["processing_errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
