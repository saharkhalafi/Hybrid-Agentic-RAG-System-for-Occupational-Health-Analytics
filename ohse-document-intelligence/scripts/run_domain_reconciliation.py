"""Apply transactional OHE6 domain reconciliation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from persistence.domain_reconciliation import (
    execute_domain_reconciliation,
    write_domain_apply_markdown,
)

OUT_JSON = Path(r"E:\temp\ohse_domain_reconciliation_apply_result.json")
OUT_MD = Path(r"E:\temp\ohse_domain_reconciliation_apply_result.md")


def main() -> None:
    result = execute_domain_reconciliation()
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_domain_apply_markdown(result, OUT_MD)
    print(
        json.dumps(
            {
                "success": result.get("success"),
                "transaction_committed": result.get("transaction_committed"),
                "rollback_performed": result.get("rollback_performed"),
                "write_stats": result.get("write_stats"),
                "post_commit_verification": {
                    "passed": (result.get("post_commit_verification") or {}).get("passed"),
                    "checks": (result.get("post_commit_verification") or {}).get("checks"),
                },
            },
            indent=2,
            default=str,
        )
    )
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
