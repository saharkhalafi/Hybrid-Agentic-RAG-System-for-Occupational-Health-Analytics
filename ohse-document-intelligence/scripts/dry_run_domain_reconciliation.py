"""Generate read-only domain reconciliation dry-run report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.domain_reconciliation import (
    generate_domain_reconciliation_report,
    write_domain_reconciliation_markdown,
)

OUT_JSON = Path(r"E:\temp\ohse_domain_reconciliation_report.json")
OUT_MD = Path(r"E:\temp\ohse_domain_reconciliation_report.md")


def main() -> None:
    report = generate_domain_reconciliation_report()
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_domain_reconciliation_markdown(report, OUT_MD)
    summary = {
        "dry_run": report.get("dry_run"),
        "oel_action_counts": report.get("oel_chemical_limits", {}).get("action_counts"),
        "old_ohe6_scope": report.get("oel_chemical_limits", {}).get("old_count_ohe6_scope"),
        "new_authoritative": report.get("oel_chemical_limits", {}).get("new_authoritative_count"),
        "stale_without_source_row_key": report.get("oel_chemical_limits", {}).get(
            "stale_without_source_row_key_count"
        ),
    }
    print(json.dumps(summary, indent=2))
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")


if __name__ == "__main__":
    main()
