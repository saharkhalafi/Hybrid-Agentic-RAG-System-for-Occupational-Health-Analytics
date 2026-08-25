"""P5 post-Phase-7 correctness and retrieval benchmark."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from agents.evaluation.phase_c3_eval import benchmark_retrieval_modes
from agents.orchestrator.pipeline import QueryOrchestrator
from agents.session.store import SessionStore
from config.settings import PROJECT_ROOT
from database.models import Document
from database.session import SessionLocal
from persistence.domain_reconciliation import OHE6_CONTENT_HASH
from persistence.phase7_chunk_rebuild import verify_phase7_applied, verify_phase7_sql_checks
from persistence.semantic_store import PRODUCTION_JSONL
from tests.test_structured_authority import CORRECTNESS_MATRIX, STATIC_CORRECTNESS_MATRIX

OUT_DIR = PROJECT_ROOT / "data" / "evaluation" / "p5_results"


@dataclass
class P5BenchmarkReport:
    generated_at: str = ""
    phase7_sql_checks: dict[str, Any] = field(default_factory=dict)
    phase7_verification: dict[str, Any] = field(default_factory=dict)
    e2e_matrix: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "phase7_sql_checks": self.phase7_sql_checks,
            "phase7_verification": self.phase7_verification,
            "e2e_matrix": self.e2e_matrix,
            "retrieval": self.retrieval,
            "acceptance": self.acceptance,
        }


def _case_payload(case_param) -> dict[str, Any]:
    return case_param.values[0] if hasattr(case_param, "values") else case_param


def _matrix_category(case: dict[str, Any]) -> str:
    case_id = case.get("session_id", "")
    if case.get("expect_no_data"):
        return "legacy_only"
    if "matrix-canonical-" in case_id:
        return "canonical_newly_promoted"
    if any(case_id == _case_payload(p).get("session_id") for p in STATIC_CORRECTNESS_MATRIX[:5]):
        return "canonical_pre_f02"
    return "canonical_other"


def _load_production_chunk_ids(settings) -> set[str]:
    production_path = settings.gold_dir / "rag" / PRODUCTION_JSONL
    chunk_ids: set[str] = set()
    if not production_path.exists():
        return chunk_ids
    for line in production_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        chunk_id = record.get("chunk_id")
        if chunk_id:
            chunk_ids.add(str(chunk_id))
    return chunk_ids


def run_e2e_matrix_benchmark(session) -> dict[str, Any]:
    orch = QueryOrchestrator(session, session_store=SessionStore())
    categories: dict[str, dict[str, int]] = {}
    wrong_chemical: list[dict[str, Any]] = []
    no_data_missed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total = 0

    for case_param in CORRECTNESS_MATRIX:
        case = _case_payload(case_param)
        category = _matrix_category(case)
        categories.setdefault(category, {"total": 0, "passed": 0})
        categories[category]["total"] += 1
        total += 1

        resp = orch.handle(case["query"], session_id=f"p5-{case['session_id']}")
        trace = resp.metadata.get("trace", {})
        slots = trace.get("slots", {})
        passed = True

        if case.get("expect_cas") and slots.get("cas") != case["expect_cas"]:
            wrong_chemical.append(
                {
                    "case": case_param.id if hasattr(case_param, "id") else case["session_id"],
                    "expected_cas": case["expect_cas"],
                    "actual_cas": slots.get("cas"),
                    "query": case["query"],
                }
            )
            passed = False

        if case.get("forbidden_cas") and slots.get("cas") in case["forbidden_cas"]:
            wrong_chemical.append(
                {
                    "case": case_param.id if hasattr(case_param, "id") else case["session_id"],
                    "forbidden_cas": case["forbidden_cas"],
                    "actual_cas": slots.get("cas"),
                    "query": case["query"],
                }
            )
            passed = False

        if case.get("expect_structured_success") is True:
            if trace.get("guardrail_decision") != "pass":
                failures.append(
                    {
                        "case": case_param.id if hasattr(case_param, "id") else case["session_id"],
                        "reason": trace.get("guardrail_decision"),
                        "query": case["query"],
                    }
                )
                passed = False

        if case.get("expect_no_data"):
            if trace.get("guardrail_decision") != "no_data":
                no_data_missed.append(
                    {
                        "case": case_param.id if hasattr(case_param, "id") else case["session_id"],
                        "decision": trace.get("guardrail_decision"),
                        "query": case["query"],
                    }
                )
                passed = False
            elif case.get("expect_no_data_reason"):
                reason = resp.metadata.get("no_data_reason") or trace.get("no_data_reason")
                if reason != case["expect_no_data_reason"]:
                    no_data_missed.append(
                        {
                            "case": case_param.id if hasattr(case_param, "id") else case["session_id"],
                            "expected_reason": case["expect_no_data_reason"],
                            "actual_reason": reason,
                            "query": case["query"],
                        }
                    )
                    passed = False

        if passed:
            categories[category]["passed"] += 1

    legacy = categories.get("legacy_only", {})
    legacy_total = legacy.get("total", 0)
    legacy_passed = legacy.get("passed", 0)

    return {
        "total_cases": total,
        "wrong_chemical_count": len(wrong_chemical),
        "wrong_chemical_rate": len(wrong_chemical) / total if total else 0.0,
        "wrong_chemical_cases": wrong_chemical,
        "no_data_missed_count": len(no_data_missed),
        "no_data_missed_cases": no_data_missed,
        "other_failures": failures,
        "by_category": {
            cat: {
                **stats,
                "pass_rate": stats["passed"] / stats["total"] if stats["total"] else 0.0,
            }
            for cat, stats in categories.items()
        },
        "legacy_only_no_data_correctness": (
            legacy_passed / legacy_total if legacy_total else 1.0
        ),
    }


def run_p5_benchmark(*, retrieval_limit: int | None = 30) -> P5BenchmarkReport:
    from config.settings import get_settings

    settings = get_settings()
    report = P5BenchmarkReport(generated_at=datetime.now(timezone.utc).isoformat())
    session = SessionLocal()

    try:
        document = session.scalar(select(Document).where(Document.content_hash == OHE6_CONTENT_HASH))
        if not document:
            raise RuntimeError("Canonical OHE6 document not found")

        production_chunk_ids = _load_production_chunk_ids(settings)
        report.phase7_verification = verify_phase7_applied(
            session,
            document_id=document.id,
            production_chunk_ids=production_chunk_ids,
        )
        report.phase7_sql_checks = verify_phase7_sql_checks(session, document_id=document.id)

        t0 = time.perf_counter()
        report.e2e_matrix = run_e2e_matrix_benchmark(session)
        report.e2e_matrix["latency_seconds"] = round(time.perf_counter() - t0, 2)

        if retrieval_limit:
            t1 = time.perf_counter()
            report.retrieval = benchmark_retrieval_modes(session, limit=retrieval_limit)
            report.retrieval["latency_seconds"] = round(time.perf_counter() - t1, 2)

        report.acceptance = {
            "wrong_chemical_rate_target": 0.0,
            "wrong_chemical_rate_pass": report.e2e_matrix.get("wrong_chemical_rate", 1.0) == 0.0,
            "legacy_no_data_target": 1.0,
            "legacy_no_data_pass": report.e2e_matrix.get("legacy_only_no_data_correctness", 0.0) >= 1.0,
            "phase7_sql_checks_pass": report.phase7_sql_checks.get("passed", False),
            "phase7_verification_pass": report.phase7_verification.get("passed", False),
            "retrieval_recall_at_5_best": max(
                (v.get("recall_at_5", 0.0) for k, v in report.retrieval.items() if k != "latency_seconds"),
                default=0.0,
            ),
        }
    finally:
        session.close()

    return report


def write_report(report: P5BenchmarkReport, path: Path | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = path or OUT_DIR / "p5_correctness_benchmark.json"
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    md = OUT_DIR / "p5_correctness_benchmark.md"
    e2e = report.e2e_matrix
    lines = [
        "# P5 Correctness & Retrieval Benchmark",
        "",
        f"Generated: {report.generated_at}",
        "",
        "## Phase 7 verification",
        "",
        f"- SQL checks passed: **{report.phase7_sql_checks.get('passed')}**",
        f"- Full verification passed: **{report.phase7_verification.get('passed')}**",
        "",
        "## E2E correctness matrix",
        "",
        f"- Total cases: **{e2e.get('total_cases', 0)}**",
        f"- Wrong-chemical rate: **{e2e.get('wrong_chemical_rate', 0):.1%}** ({e2e.get('wrong_chemical_count', 0)} cases)",
        f"- Legacy-only no_data correctness: **{e2e.get('legacy_only_no_data_correctness', 0):.1%}**",
        "",
        "### By category",
        "",
    ]
    for cat, stats in (e2e.get("by_category") or {}).items():
        lines.append(f"- **{cat}**: {stats.get('passed')}/{stats.get('total')} ({stats.get('pass_rate', 0):.1%})")
    lines.extend(["", "## Retrieval (Recall@5)", ""])
    for mode, stats in report.retrieval.items():
        if mode == "latency_seconds":
            continue
        lines.append(
            f"- **{mode}**: Recall@5={stats.get('recall_at_5', 0):.1%}, "
            f"MRR={stats.get('mrr', 0):.3f}, "
            f"p50={stats.get('p50_latency_ms', 0):.0f}ms, "
            f"p95={stats.get('p95_latency_ms', 0):.0f}ms, "
            f"n={stats.get('n_queries', 0)}"
        )
    lines.extend(["", "## Acceptance", ""])
    for key, val in report.acceptance.items():
        lines.append(f"- {key}: **{val}**")
    md.write_text("\n".join(lines), encoding="utf-8")
    return out
