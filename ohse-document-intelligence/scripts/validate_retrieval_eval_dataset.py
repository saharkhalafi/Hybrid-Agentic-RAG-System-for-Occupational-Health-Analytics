#!/usr/bin/env python3
"""Validate retrieval evaluation dataset; exit non-zero on critical errors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the project root is importable when this script is executed directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from retrieval.eval_schema import ReviewStatus
from retrieval.eval_validator import CRITICAL, validate_master


OUT = PROJECT_ROOT / "data" / "retrieval_eval" / "validation_report.json"


def main() -> int:
    records, issues = validate_master()

    critical = [i for i in issues if i["level"] == CRITICAL]
    warnings = [i for i in issues if i["level"] != CRITICAL]

    report = {
        "total_records": len(records),
        "critical_errors": len(critical),
        "warnings": len(warnings),
        "issues": issues,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "records": len(records),
                "critical": len(critical),
                "warnings": len(warnings),
            },
            indent=2,
        )
    )

    if critical:
        for item in critical[:20]:
            print(
                f"CRITICAL [{item['query_id']}] "
                f"{item['code']}: {item['message']}"
            )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

