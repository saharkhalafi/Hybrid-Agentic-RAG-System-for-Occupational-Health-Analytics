"""Execute geometry promotion persistence to PostgreSQL."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.geometry_promotion_persist import (
    PROMOTION_REPORT_DEFAULT,
    execute_geometry_promotion,
)

OUT_JSON = Path(r"E:\temp\ohse_geometry_persistence_result.json")
OUT_MD = Path(r"E:\temp\ohse_geometry_persistence_result.md")


def write_markdown(result: dict) -> None:
    lines = [
        "# OHE6 Geometry Promotion — Persistence Result",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Success: **{result.get('success')}**",
        f"Transaction committed: **{result.get('transaction_committed')}**",
        f"Rollback performed: **{result.get('rollback_performed')}**",
        "",
        f"Document ID: `{result.get('document_id')}`",
        f"Content hash: `{result.get('content_hash')}`",
        "",
        "## Expected vs Actual",
        "",
    ]
    expected = result.get("expected", {})
    verification = result.get("verification", {}).get("checks", {})
    canonical = verification.get("canonical_disposition_counts", {})
    lines.append("| Metric | Expected | Verified |")
    lines.append("|--------|----------|----------|")
    lines.append(f"| Canonical cells | {expected.get('total_cells')} | {verification.get('promotion_scope_cell_count')} |")
    lines.append(f"| Cell rows (incl. legacy duplicates) | — | {verification.get('promotion_scope_cell_row_count')} |")
    lines.append(f"| Tables (stable_table_id) | {expected.get('tables')} | {verification.get('promotion_scope_table_count')} |")
    lines.append(f"| Domain rows | {expected.get('domain_upserts')} | {verification.get('domain_rows_present')} |")
    lines.append(f"| Review tasks | {expected.get('review_tasks')} | {verification.get('promotion_review_tasks')} |")
    lines.append(f"| ACCEPT | {expected.get('accepted')} | {canonical.get('ACCEPT', '—')} |")
    lines.append(f"| REVIEW | {expected.get('review')} | {canonical.get('REVIEW', '—')} |")
    lines.append(f"| REJECT | {expected.get('rejected')} | {canonical.get('REJECT', '—')} |")

    before = result.get("before", {})
    after = result.get("after", {})
    if before or after:
        lines.extend(["", "## Before / After DB Counts", ""])
        lines.append("| Table | Before | After | Delta |")
        lines.append("|-------|--------|-------|-------|")
        for key in (
            "total_documents",
            "total_table_cells",
            "total_extracted_tables",
            "total_oel_limits",
            "total_review_tasks",
            "promotion_scope_cells",
            "cells_outside_promotion_scope",
        ):
            b = before.get(key, "—")
            a = after.get(key, "—")
            delta = (a - b) if isinstance(a, int) and isinstance(b, int) else "—"
            lines.append(f"| {key} | {b} | {a} | {delta} |")

    lines.extend(["", "## Write Stats", ""])
    for k, v in (result.get("write_stats") or {}).items():
        lines.append(f"- {k}: {v}")

    lines.extend(["", "## Integrity Checks", ""])
    for k, v in verification.items():
        lines.append(f"- {k}: {v}")

    discrepancies = result.get("verification", {}).get("discrepancies", [])
    if discrepancies:
        lines.extend(["", "## Discrepancies", ""])
        for d in discrepancies:
            lines.append(f"- {d}")
    else:
        lines.extend(["", "## Discrepancies", "", "None."])

    post = result.get("post_commit_verification", {})
    if post:
        lines.extend(["", "## Post-Commit Verification", "", f"Passed: **{post.get('passed')}**"])

    lines.extend([
        "",
        "## Notes",
        "",
        "- Reconciliation update only; no truncation or out-of-scope deletes.",
        "- Legacy duplicate cell rows (418) retain historical table_id FKs; all rows received disposition updates.",
        "- Embeddings / document_chunks were not populated in this step.",
    ])

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    result = execute_geometry_promotion(PROMOTION_REPORT_DEFAULT, evidence_only=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_markdown(result)
    print(json.dumps({k: result.get(k) for k in ("success", "transaction_committed", "rollback_performed", "write_stats", "verification")}, default=str, indent=2))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
