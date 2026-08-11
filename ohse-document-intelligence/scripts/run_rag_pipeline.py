"""Run full RAG-ready pipeline for pages 44-49."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> None:
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Goldset + RAG corpus + optional Postgres")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--start-page", type=int, default=44)
    parser.add_argument("--end-page", type=int, default=49)
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    gold_cmd = [
        sys.executable,
        "scripts/generate_goldset.py",
        "--file",
        str(args.file),
        "--start-page",
        str(args.start_page),
        "--end-page",
        str(args.end_page),
    ]
    if args.force:
        gold_cmd.append("--force")
    _run(gold_cmd)

    _run(
        [
            sys.executable,
            "scripts/build_rag_corpus.py",
            "--start-page",
            str(args.start_page),
            "--end-page",
            str(args.end_page),
        ]
    )

    if not args.skip_postgres:
        try:
            _run([sys.executable, "-m", "alembic", "upgrade", "head"])
            _run(
                [
                    sys.executable,
                    "scripts/persist_evidence_pipeline.py",
                    "--file",
                    str(args.file),
                    "--start-page",
                    str(args.start_page),
                    "--end-page",
                    str(args.end_page),
                ]
            )
            _run([sys.executable, "scripts/load_rag_to_postgres.py"])
        except SystemExit:
            print("\nPostgreSQL step skipped or failed — RAG JSONL corpus is ready under gold/rag/")


if __name__ == "__main__":
    main()
