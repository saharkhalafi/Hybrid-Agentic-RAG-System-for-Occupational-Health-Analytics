"""Read-only summary of an existing Goldset QA JSON report."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = PROJECT / "data" / "evaluation" / "goldset_qa_report.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize a Goldset QA evaluation JSON report")
    parser.add_argument(
        "report",
        nargs="?",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to goldset_qa_report.json",
    )
    return parser.parse_args(argv)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def fmt_metric(block: Any, key: str) -> str:
    if not isinstance(block, dict):
        return "-"
    n = _num(block.get(key))
    if n is None:
        return "-"
    return f"{n:.3f}"


def rank_from_metrics(block: Any) -> str:
    """First relevant rank implied by stored MRR (1/MRR). No relevance recomputation."""
    if not isinstance(block, dict):
        return "-"
    hit1 = _num(block.get("hit@1"))
    if hit1 is not None and hit1 >= 1.0:
        return "1"
    mrr = _num(block.get("mrr"))
    if mrr is None or mrr <= 0:
        return "-"
    rank = int(round(1.0 / mrr))
    return str(rank) if rank >= 1 else "-"


def agents_of(case: dict[str, Any]) -> list[str]:
    agents = case.get("actual_agents")
    if not isinstance(agents, list):
        diag = case.get("diagnostics") if isinstance(case.get("diagnostics"), dict) else {}
        agents = diag.get("actual_agents") if isinstance(diag, dict) else []
    if not isinstance(agents, list):
        return []
    return [str(a) for a in agents]


def intent_of(case: dict[str, Any]) -> str:
    intent = case.get("actual_intent")
    if intent in (None, ""):
        diag = case.get("diagnostics") if isinstance(case.get("diagnostics"), dict) else {}
        intent = diag.get("actual_intent") if isinstance(diag, dict) else None
    return str(intent) if intent not in (None, "") else "-"


def families_of(case: dict[str, Any]) -> list[str]:
    fam = case.get("metric_families")
    if isinstance(fam, list) and fam:
        return [str(x) for x in fam]
    found: list[str] = []
    for name in ("semantic", "structured", "formula"):
        if isinstance(case.get(name), dict):
            found.append(name)
    return found


def is_clarify_or_guardrail(case: dict[str, Any]) -> bool:
    intent = intent_of(case)
    if intent.startswith("CLARIFY.") or intent.startswith("GUARDRAIL."):
        return True
    agents = {a.lower() for a in agents_of(case)}
    return bool(agents & {"clarify", "guardrail"})


def type_row_metrics(qtype: str, entry: dict[str, Any]) -> dict[str, Any]:
    if qtype == "formula":
        block = entry.get("formula") if isinstance(entry.get("formula"), dict) else None
    else:
        block = entry.get("semantic") if isinstance(entry.get("semantic"), dict) else None
    return {
        "n": entry.get("n", "-"),
        "hit@1": fmt_metric(block, "hit@1"),
        "hit@3": fmt_metric(block, "hit@3"),
        "hit@5": fmt_metric(block, "hit@5"),
        "mrr": fmt_metric(block, "mrr"),
    }


def failure_rows(cases: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        families = set(families_of(case))
        reasons: list[str] = []
        rank_src: Any = None

        if case.get("no_result") is True:
            reasons.append("no_result")

        if "semantic" in families:
            sem = case.get("semantic") if isinstance(case.get("semantic"), dict) else None
            hit = _num((sem or {}).get("hit@1")) if sem else None
            if sem is None or hit == 0:
                reasons.append("sem@1=0")
                rank_src = rank_src or sem

        if "structured" in families:
            st = case.get("structured") if isinstance(case.get("structured"), dict) else None
            hit = _num((st or {}).get("hit@1")) if st else None
            if st is None or hit == 0:
                reasons.append("str@1=0")
                rank_src = rank_src or st

        if "formula" in families:
            fo = case.get("formula") if isinstance(case.get("formula"), dict) else None
            hit = _num((fo or {}).get("hit@1")) if fo else None
            if fo is None or hit == 0:
                reasons.append("formula_fail")
                rank_src = rank_src or fo

        if is_clarify_or_guardrail(case):
            reasons.append("clarify/guardrail")

        if not reasons:
            continue

        gid = str(case.get("gold_id") or "-")
        qtype = str(case.get("question_type") or "-")
        intent = intent_of(case)
        agent = ",".join(agents_of(case)) or "-"
        status = ",".join(reasons)
        rank = rank_from_metrics(rank_src)
        rows.append((gid, qtype, intent, agent, status, rank))
    return rows


def pad(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c[:w].ljust(w) for c, w in zip(cols, widths, strict=True))


def print_table(headers: list[str], rows: list[list[str]], *, max_col: int = 72) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(len(cell), max_col))
    print(pad(headers, widths))
    print(pad(["-" * w for w in widths], widths))
    for row in rows:
        print(pad(row, widths))


def summarize(report: dict[str, Any]) -> None:
    cases = report.get("cases") if isinstance(report.get("cases"), list) else []
    cases = [c for c in cases if isinstance(c, dict)]
    n_total = report.get("n_items")
    if n_total is None:
        n_total = len(cases)

    n_no_result = sum(1 for c in cases if c.get("no_result") is True)
    n_sem = n_str = n_form = n_other = 0
    for c in cases:
        agents = {a.lower() for a in agents_of(c)}
        if "semantic" in agents:
            n_sem += 1
        if "structured" in agents:
            n_str += 1
        if "formula" in agents:
            n_form += 1
        if is_clarify_or_guardrail(c) or not (agents & {"semantic", "structured", "formula"}):
            n_other += 1

    print("=== GOLDSET QA SUMMARY ===")
    print()
    print("1. Overall")
    print()
    print(f"* Total questions: {n_total}")
    print(f"* Questions with no_result: {n_no_result}")
    print(f"* Questions routed to semantic: {n_sem}")
    print(f"* Questions routed to structured: {n_str}")
    print(f"* Questions routed to formula: {n_form}")
    print(f"* Questions routed to clarify/guardrail/other: {n_other}")
    print()
    print("2. Semantic metrics")
    print()
    sem = report.get("semantic")
    print(f"* Hit@1:  {fmt_metric(sem, 'hit@1')}")
    print(f"* Hit@3:  {fmt_metric(sem, 'hit@3')}")
    print(f"* Hit@5:  {fmt_metric(sem, 'hit@5')}")
    print(f"* Hit@10: {fmt_metric(sem, 'hit@10')}")
    print(f"* MRR:    {fmt_metric(sem, 'mrr')}")
    print()
    print("3. Structured metrics")
    print()
    st = report.get("structured")
    print(f"* Hit@1: {fmt_metric(st, 'hit@1')}")
    print(f"* MRR:   {fmt_metric(st, 'mrr')}")
    print()
    print("4. By question type")
    print()
    by_type = report.get("by_question_type") if isinstance(report.get("by_question_type"), dict) else {}
    rows = []
    for qtype, entry in by_type.items():
        if not isinstance(entry, dict):
            rows.append([str(qtype), "-", "-", "-", "-", "-"])
            continue
        m = type_row_metrics(str(qtype), entry)
        rows.append([str(qtype), str(m["n"]), m["hit@1"], m["hit@3"], m["hit@5"], m["mrr"]])
    print_table(["Type", "N", "Hit@1", "Hit@3", "Hit@5", "MRR"], rows)
    print()
    print("5. Routing/result failures")
    print()
    fails = failure_rows(cases)
    if not fails:
        print("(none)")
    else:
        print_table(
            ["ID", "Type", "Intent", "Agent", "Status", "Rank"],
            [list(r) for r in fails],
        )
        print(f"({len(fails)} questions)")
    print()
    print("6. Routing distribution")
    print()
    intent_counts: Counter[str] = Counter()
    agent_counts: Counter[str] = Counter()
    combo_counts: Counter[str] = Counter()
    for c in cases:
        intent = intent_of(c)
        agents = agents_of(c)
        agent_key = ",".join(agents) if agents else "-"
        intent_counts[intent] += 1
        combo_counts[f"{intent} / {agent_key}"] += 1
        if agents:
            for a in agents:
                agent_counts[a] += 1
        else:
            agent_counts["-"] += 1
    print_table(
        ["Intent/Agent", "Count"],
        [[k, str(v)] for k, v in combo_counts.most_common()],
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.report
    if not path.is_file():
        print(f"Report not found: {path}", flush=True)
        return 1
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        print("Report JSON is not an object.", flush=True)
        return 1
    summarize(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
