"""Report OEL rows blocked from accepted status — by page, table, and reason."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.domain_reconciliation import (  # noqa: E402
    OHE6_CONTENT_HASH,
    build_canonical_oel_from_cells,
    build_reconciliation_plan,
    load_canonical_cells_from_db,
    load_promotion_report,
    PROMOTION_REPORT_DEFAULT,
    verify_domain_reconciliation_applied,
)
from database.session import SessionLocal  # noqa: E402
from sqlalchemy import select  # noqa: E402
from database.models import Document, OELChemicalLimit  # noqa: E402

OUT_JSON = Path(r"E:\temp\ohse_oel_review_blockers_report.json")
OUT_MD = Path(r"E:\temp\ohse_oel_review_blockers_report.md")


def main() -> None:
    session = SessionLocal()
    try:
        document = session.scalar(select(Document).where(Document.content_hash == OHE6_CONTENT_HASH))
        if not document:
            raise RuntimeError(f"Document not found for content_hash={OHE6_CONTENT_HASH}")

        report = load_promotion_report(PROMOTION_REPORT_DEFAULT)
        promotion_cell_ids = {cell["cell_id"] for cell in report.get("cells", [])}
        promotion_table_ids = {cell["table_id"] for cell in report.get("cells", [])}

        cells = load_canonical_cells_from_db(
            session,
            document_id=document.id,
            promotion_cell_ids=promotion_cell_ids,
            content_hash=OHE6_CONTENT_HASH,
        )
        authoritative, review_only = build_canonical_oel_from_cells(cells)
        _cells, _auth, _rev, entries, _ = build_reconciliation_plan(
            session,
            document_id=document.id,
            content_hash=OHE6_CONTENT_HASH,
        )

        auth_keys = {r.source_row_key for r in authoritative}
        verify = verify_domain_reconciliation_applied(
            session,
            promotion_table_ids=promotion_table_ids,
            authoritative_keys=auth_keys,
            expected_counts={
                "INSERT": 0,
                "UPDATE": 0,
                "RETIRE": sum(1 for e in entries if e.action == "RETIRE"),
                "REVIEW": sum(1 for e in entries if e.action == "REVIEW"),
                "authoritative": len(authoritative),
            },
        )

        db_review = session.scalars(
            select(OELChemicalLimit).where(OELChemicalLimit.validation_status == "review_required")
        ).all()

        by_reason: dict[str, list[dict]] = defaultdict(list)
        by_table: dict[str, Counter] = defaultdict(Counter)
        by_page: Counter = Counter()

        for row in review_only:
            item = {
                "source_row_key": row.source_row_key,
                "stable_table_id": row.stable_table_id,
                "page_number": row.page_number,
                "row_index": row.row_index,
                "cas": row.cas or None,
                "english_name": (row.english_name or "")[:120],
                "review_reason": row.review_reason,
            }
            by_reason[row.review_reason or "unknown"].append(item)
            by_table[row.stable_table_id][row.review_reason or "unknown"] += 1
            by_page[row.page_number] += 1

        table_summary = []
        for table_id, reasons in sorted(by_table.items()):
            page = next((r.page_number for r in review_only if r.stable_table_id == table_id), None)
            table_summary.append({
                "table_id": table_id,
                "page_number": page,
                "blocked_rows": sum(reasons.values()),
                "reasons": dict(reasons),
            })
        table_summary.sort(key=lambda x: (-x["blocked_rows"], x["table_id"]))

        page_summary = [
            {"page_number": page, "blocked_rows": count}
            for page, count in sorted(by_page.items())
        ]

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reconciliation_already_applied": verify["passed"],
            "reconciliation_checks": verify["checks"],
            "summary": {
                "promoted_cells": len(cells),
                "promoted_tables": len(promotion_table_ids),
                "authoritative_accepted": len(authoritative),
                "canonical_review_blocked": len(review_only),
                "db_review_required_rows": len(db_review),
                "review_reason_counts": dict(Counter(r.review_reason for r in review_only)),
            },
            "by_page": page_summary,
            "by_table": table_summary,
            "by_reason": {k: v for k, v in sorted(by_reason.items())},
            "db_review_required_sample": [
                {
                    "source_row_key": r.source_row_key,
                    "page_number": r.page_number,
                    "cas": getattr(getattr(r, "chemical", None), "cas", None),
                    "english_name": (r.english_name or "")[:120],
                    "validation_status": r.validation_status,
                }
                for r in db_review[:30]
            ],
            "fix_guidance": {
                "chemical_name_not_accepted": "Repair/promote chemical name cells in geometry promotion or HITL accept disposition.",
                "missing_cas": "Ensure CAS is present and extractable in the chemical name cell text.",
                "no_accept_limit": "Promote/repair TWA/STEL/Ceiling cells — name+CAS ok but no accepted numeric limit cell.",
            },
        }

        OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(payload, OUT_MD)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        print(f"Wrote {OUT_JSON}")
        print(f"Wrote {OUT_MD}")
    finally:
        session.close()


def write_markdown(payload: dict, path: Path) -> None:
    s = payload["summary"]
    lines = [
        "# OHE6 OEL Review Blockers Report",
        f"Generated: {payload['generated_at']}",
        "",
        "## Reconciliation status",
        f"- Already applied: **{payload['reconciliation_already_applied']}**",
        f"- Authoritative accepted rows: **{s['authoritative_accepted']}**",
        f"- Canonical rows blocked (need promotion/HITL): **{s['canonical_review_blocked']}**",
        f"- DB `review_required` rows: **{s['db_review_required_rows']}**",
        "",
        "## Block reasons (canonical build)",
    ]
    for reason, count in sorted(s["review_reason_counts"].items(), key=lambda x: -x[1]):
        guidance = payload["fix_guidance"].get(reason, "")
        lines.append(f"- **{reason}**: {count} rows — {guidance}")

    lines.extend(["", "## Top tables by blocked rows", ""])
    lines.append("| Page | Table | Blocked rows | Reasons |")
    lines.append("|------|-------|--------------|---------|")
    for row in payload["by_table"][:25]:
        reasons = ", ".join(f"{k}:{v}" for k, v in sorted(row["reasons"].items(), key=lambda x: -x[1]))
        lines.append(
            f"| {row['page_number']} | `{row['table_id']}` | {row['blocked_rows']} | {reasons} |"
        )

    lines.extend(["", "## Pages with most blocked rows", ""])
    lines.append("| Page | Blocked rows |")
    lines.append("|------|--------------|")
    for row in payload["by_page"][:20]:
        lines.append(f"| {row['page_number']} | {row['blocked_rows']} |")

    lines.extend([
        "",
        "## Next steps",
        "1. Fix geometry promotion / OCR on blocked name and limit cells.",
        "2. Re-run geometry promotion for affected tables.",
        "3. Re-run `dry_run_domain_reconciliation.py` — expect more `accepted`, fewer blocked.",
        "4. Apply reconciliation only if dry-run action counts change.",
        "",
        f"Full JSON: `{OUT_JSON}`",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
