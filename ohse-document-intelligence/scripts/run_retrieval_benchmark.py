"""Retrieval-only benchmark (skips E2E matrix) for post-fix MRR/latency reporting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agents.evaluation.phase_c3_eval import benchmark_retrieval_modes
from database.session import SessionLocal


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    session = SessionLocal()
    try:
        results = benchmark_retrieval_modes(session, limit=args.limit)
    finally:
        session.close()

    out = PROJECT / "data" / "evaluation" / "p5_results" / "p5_retrieval_benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
