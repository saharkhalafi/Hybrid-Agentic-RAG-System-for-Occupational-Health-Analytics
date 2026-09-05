"""CLI for Goldset QA evaluation. Do not run the full 50-query set from Phase 3B tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from agents.evaluation.goldset_qa_eval import GoldsetQAEvaluator
from agents.evaluation.goldset_qa_loader import default_goldset_qa_path, load_goldset_qa
from agents.orchestrator.pipeline import QueryOrchestrator
from database.session import SessionLocal

DEFAULT_BEFORE = PROJECT / "data" / "evaluation" / "goldset_qa_after_f7_structured.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate production QueryOrchestrator against frozen Goldset_QA.xlsx"
    )
    parser.add_argument(
        "--goldset",
        type=Path,
        default=None,
        help="Path to Goldset_QA.xlsx (default: ohse-document-intelligence/Goldset_QA.xlsx)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N rows")
    parser.add_argument("--ids", type=str, default=None, help="Comma-separated Gold ids (e.g. Q01,Q20)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON report to this path",
    )
    parser.add_argument(
        "--before",
        type=Path,
        default=DEFAULT_BEFORE,
        help="Previous 50-question JSON report for Before/After comparison",
    )
    parser.add_argument(
        "--chart",
        type=Path,
        default=None,
        help="Write Before/After SVG chart (default: next to --out)",
    )
    return parser.parse_args(argv)


def _num(block: dict | None, key: str) -> float:
    if not isinstance(block, dict):
        return 0.0
    val = block.get(key)
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def unified_from_legacy_case(case: dict) -> dict[str, float]:
    """Approximate overall hits from frozen family metrics (NoOp pre-rerank == final)."""
    families = [case.get(name) for name in ("semantic", "structured", "formula")]
    families = [f for f in families if isinstance(f, dict)]
    out: dict[str, float] = {}
    for key in ("hit@1", "hit@3", "hit@5", "mrr"):
        vals = [_num(f, key if key in f else ("hit@1" if key.startswith("hit@") else key)) for f in families]
        out[key] = max(vals) if vals else 0.0
    return out


def aggregate_legacy_unified(report: dict) -> dict[str, float]:
    cases = [c for c in (report.get("cases") or []) if isinstance(c, dict)]
    if not cases:
        return {"hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "mrr": 0.0}
    rows = [unified_from_legacy_case(c) for c in cases]
    n = len(rows)
    return {k: sum(r[k] for r in rows) / n for k in ("hit@1", "hit@3", "hit@5", "mrr")}


def comparison_table(before: dict[str, float], after_pre: dict, after_final: dict) -> list[tuple[str, float, float, float]]:
    rows = []
    mapping = [
        ("Pre-Rerank Hit@1", "hit@1", before, after_pre),
        ("Pre-Rerank Hit@3", "hit@3", before, after_pre),
        ("Pre-Rerank Hit@5", "hit@5", before, after_pre),
        ("Final Hit@1", "hit@1", before, after_final),
        ("Final Hit@3", "hit@3", before, after_final),
        ("Final Hit@5", "hit@5", before, after_final),
        ("MRR", "mrr", before, after_final),
    ]
    for label, key, src_before, src_after in mapping:
        b = _num(src_before, key)
        a = _num(src_after, key)
        rows.append((label, b, a, a - b))
    return rows


def write_svg_chart(path: Path, rows: list[tuple[str, float, float, float]]) -> None:
    width = 760
    height = 420
    left = 200
    bar_h = 18
    gap = 36
    top = 40
    max_v = 1.0
    scale = 480
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="24" y="24" font-family="Segoe UI, sans-serif" font-size="16">Goldset QA Before vs After</text>',
        '<rect x="520" y="10" width="12" height="12" fill="#8aa4b8"/>',
        '<text x="536" y="20" font-family="Segoe UI, sans-serif" font-size="11">Before</text>',
        '<rect x="590" y="10" width="12" height="12" fill="#2f6f4e"/>',
        '<text x="606" y="20" font-family="Segoe UI, sans-serif" font-size="11">After</text>',
    ]
    for i, (label, before, after, _delta) in enumerate(rows):
        y = top + i * gap
        parts.append(
            f'<text x="8" y="{y + 14}" font-family="Segoe UI, sans-serif" font-size="12">{label}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y}" width="{before / max_v * scale:.1f}" height="{bar_h}" fill="#8aa4b8"/>'
        )
        parts.append(
            f'<rect x="{left}" y="{y + bar_h}" width="{after / max_v * scale:.1f}" height="{bar_h}" fill="#2f6f4e"/>'
        )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_markdown(
    path: Path,
    rows: list[tuple[str, float, float, float]],
    failures: dict,
    *,
    family_rows: list[tuple[str, str, float, float]] | None = None,
) -> None:
    lines = [
        "# Goldset QA — evidence pipeline Before/After",
        "",
        "| Metric           | Before | After | Delta |",
        "| ---------------- | -----: | ----: | ----: |",
    ]
    for label, before, after, delta in rows:
        lines.append(f"| {label:<16} | {before:6.3f} | {after:5.3f} | {delta:+.3f} |")
    lines.extend(["", "## Remaining failures", ""])
    if failures:
        for name, count in sorted(failures.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")
    if family_rows:
        lines.extend(
            [
                "",
                "## Family metrics (evaluator unchanged)",
                "",
                "| Family     | Metric | Before (F7) | After |",
                "| ---------- | ------ | ----------: | ----: |",
            ]
        )
        for family, metric, before, after in family_rows:
            lines.append(f"| {family:<10} | {metric:<6} | {before:11.3f} | {after:.3f} |")
    lines.extend(
        [
            "",
            "Pre-rerank is the semantic_text vector backbone plus appended extras.",
            "Final is after identity-aware rerank. Structured Hit@1 is the structured agent, not global promotion.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    items = load_goldset_qa(args.goldset or default_goldset_qa_path())
    if args.ids:
        wanted = {part.strip() for part in args.ids.split(",") if part.strip()}
        items = [row for row in items if row.id in wanted]
    if args.limit is not None:
        items = items[: args.limit]

    session = SessionLocal()
    try:
        evaluator = GoldsetQAEvaluator(QueryOrchestrator(session))
        report = evaluator.evaluate_items(items)
    finally:
        session.close()

    if args.before and args.before.is_file():
        before_report = json.loads(args.before.read_text(encoding="utf-8"))
        before_unified = aggregate_legacy_unified(before_report)
    else:
        before_unified = {"hit@1": 0.0, "hit@3": 0.0, "hit@5": 0.0, "mrr": 0.0}

    after_pre = report.get("pre_rerank") or {}
    after_final = report.get("unified_final") or {}
    rows = comparison_table(before_unified, after_pre, after_final)
    report["before_after"] = {
        "before": before_unified,
        "after_pre_rerank": after_pre,
        "after_final": after_final,
        "rows": [
            {"metric": label, "before": b, "after": a, "delta": d} for label, b, a, d in rows
        ],
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
        chart = args.chart or args.out.with_name("goldset_qa_before_after.svg")
        write_svg_chart(chart, rows)
        family_rows: list[tuple[str, str, float, float]] = []
        before_report = {}
        if args.before and args.before.is_file():
            before_report = json.loads(args.before.read_text(encoding="utf-8"))
        for family, keys in (
            ("semantic", ("hit@1", "hit@5", "mrr")),
            ("structured", ("hit@1",)),
        ):
            before_block = before_report.get(family) if isinstance(before_report.get(family), dict) else {}
            after_block = report.get(family) if isinstance(report.get(family), dict) else {}
            for key in keys:
                family_rows.append((family, key, _num(before_block, key), _num(after_block, key)))
        write_markdown(
            args.out.with_name("goldset_qa_before_after.md"),
            rows,
            report.get("failure_categories") or {},
            family_rows=family_rows,
        )
        print(f"Wrote {chart}")
    else:
        print(text)
    print()
    print("| Metric           | Before | After | Delta |")
    print("| ---------------- | -----: | ----: | ----: |")
    for label, before, after, delta in rows:
        print(f"| {label:<16} | {before:6.3f} | {after:5.3f} | {delta:+.3f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
