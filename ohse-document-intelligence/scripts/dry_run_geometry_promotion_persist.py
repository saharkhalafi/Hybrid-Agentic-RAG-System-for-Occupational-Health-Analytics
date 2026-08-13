"""Dry-run persistence report for geometry-promotion evidence. NO PostgreSQL writes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.geometry_promotion_persist import generate_dry_run_report

OUT_JSON = Path(r"E:\temp\ohse_geometry_persistence_dry_run.json")
OUT_MD = Path(r"E:\temp\ohse_geometry_persistence_dry_run.md")


def write_markdown(report: dict) -> None:
    t = report["totals"]
    e = report["evidence_layer"]
    d = report["domain_layer"]
    r = report["review_layer"]
    q = report["quality_checks"]
    lines = [
        "# Geometry Promotion — Dry-Run Persistence Report",
        "",
        "**NO PostgreSQL writes were performed.**",
        "",
        f"Content hash: `{report['content_hash'][:16]}…`",
        f"Document ID: `{report['document_id']}`",
        "",
        "## Totals",
        "",
        f"| Disposition | Count |",
        f"|-------------|------:|",
        f"| Total cells | {t['total_cells']} |",
        f"| Accepted | {t['accepted']} |",
        f"| Review | {t['review']} |",
        f"| Rejected | {t['rejected']} |",
        "",
        "## Evidence Layer (all cells)",
        "",
        f"- `table_cells` insert: **{e['table_cells_to_insert']}**",
        f"- `table_cells` update: **{e['table_cells_to_update']}**",
        f"- `extracted_tables` upsert: **{e['extracted_tables']}**",
        "",
        "### Rejection reasons",
        "",
    ]
    for reason, count in sorted(e["rejection_reasons"].items(), key=lambda x: -x[1]):
        lines.append(f"- {reason}: {count}")

    lines.extend(
        [
            "",
            "## Domain Layer (accepted only)",
            "",
            f"- `oel_chemical_limits` upsert: **{d['oel_chemical_limits_upsert']}**",
            f"- Skipped (non-domain): **{d['oel_chemical_limits_skip']}**",
            f"- Existing row-key conflicts: **{d['oel_existing_conflict_count']}**",
            "",
            "## Review Layer",
            "",
            f"- Review queue / HITL tasks: **{r['review_queue_items']}**",
            f"- Unique idempotency keys: **{r['review_task_idempotency_keys']}**",
            "",
            "## Embeddings",
            "",
            f"- {report['embeddings_layer']['note']}",
            "",
            "## Quality Checks",
            "",
            f"- Duplicate idempotency keys: **{len(q['duplicate_idempotency_keys'])}**",
            f"- Cells missing required fields: **{q['cells_missing_required_fields']}**",
            "",
            "### Provenance coverage",
            "",
            f"- With bbox: {q['provenance_coverage']['with_bbox']}/{t['total_cells']}",
            f"- Bbox source: `{q['provenance_coverage']['bbox_source']}`",
            f"- Input bbox source: `{q['provenance_coverage']['input_bbox_source']}`",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    report = generate_dry_run_report()
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report)
    summary = {
        "dry_run": True,
        "writes_blocked": True,
        "totals": report["totals"],
        "evidence_layer": report["evidence_layer"],
        "domain_layer": {
            "oel_upsert": report["domain_layer"]["oel_chemical_limits_upsert"],
            "oel_skip": report["domain_layer"]["oel_chemical_limits_skip"],
        },
        "review_layer": report["review_layer"],
        "quality_checks": {
            "duplicate_keys": len(report["quality_checks"]["duplicate_idempotency_keys"]),
            "missing_required": report["quality_checks"]["cells_missing_required_fields"],
        },
        "output_json": str(OUT_JSON),
        "output_md": str(OUT_MD),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
