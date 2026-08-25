"""Run P5 post-Phase-7 correctness and retrieval benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agents.evaluation.p5_benchmark import run_p5_benchmark, write_report

OUT_JSON = Path(r"E:\temp\ohse_p5_correctness_benchmark.json")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-limit", type=int, default=30)
    parser.add_argument("--skip-retrieval", action="store_true")
    args = parser.parse_args()

    limit = None if args.skip_retrieval else args.retrieval_limit
    report = run_p5_benchmark(retrieval_limit=limit)
    path = write_report(report)
    OUT_JSON.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.acceptance, indent=2))
    print(f"Report: {path}")
    if not all(
        report.acceptance.get(k)
        for k in (
            "phase7_sql_checks_pass",
            "phase7_verification_pass",
            "wrong_chemical_rate_pass",
            "legacy_no_data_pass",
        )
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
