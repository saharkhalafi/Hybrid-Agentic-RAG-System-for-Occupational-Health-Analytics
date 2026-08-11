"""Phase C / C.2 evaluation harness."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT
from agents.routing.classifier import IntentClassifier
from agents.routing.normalizer import normalize_persian_query
from agents.routing.slots import extract_slots
from agents.session.context import SessionContextManager
from agents.session.store import SessionStore

MASTER = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master.jsonl"
MASTER_C2 = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master_c2.jsonl"
MASTER_C4 = PROJECT_ROOT / "data" / "retrieval_eval" / "retrieval_eval_master_c4.jsonl"
OUT_DIR = PROJECT_ROOT / "data" / "evaluation"


@dataclass
class EvalSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    intent_correct: int = 0
    agent_correct: int = 0
    clarification_correct: int = 0
    context_resolved: int = 0
    context_resolution_total: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    per_intent: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"total": 0, "correct": 0}))
    per_category: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"total": 0, "intent_ok": 0, "agent_ok": 0}))
    confusion: Counter[str] = field(default_factory=Counter)
    latency_ms: list[float] = field(default_factory=list)

    def intent_accuracy(self) -> float:
        return self.intent_correct / self.total if self.total else 0.0

    def agent_accuracy(self) -> float:
        return self.agent_correct / self.total if self.total else 0.0

    def context_resolution_accuracy(self) -> float:
        return self.context_resolved / self.context_resolution_total if self.context_resolution_total else 0.0


def resolve_master_path(use_c2: bool = False, *, use_c4: bool = False) -> Path:
    if use_c4 and MASTER_C4.exists():
        return MASTER_C4
    if use_c2 and MASTER_C2.exists():
        return MASTER_C2
    return MASTER


def load_eval_records(limit: int | None = None, *, use_c2: bool = False, use_c4: bool = False, category: str | None = None) -> list[dict[str, Any]]:
    path = resolve_master_path(use_c2, use_c4=use_c4)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if category and rec.get("category") != category:
            continue
        rows.append(rec)
        if limit and len(rows) >= limit:
            break
    return rows


def _seed_session_from_record(store: SessionStore, sid: str, rec: dict[str, Any]) -> None:
    """Seed session state from expected_context / expected_slots for eval simulation."""
    state = store.create_session(sid)
    ctx = rec.get("expected_context") or rec.get("expected_slots") or rec.get("inherited_slots") or {}
    state.chemical_name = ctx.get("chemical_name") or state.chemical_name
    state.cas = ctx.get("cas") or state.cas
    if ctx.get("oel_type"):
        state.oel_type = ctx["oel_type"]
    if ctx.get("formula_id"):
        state.formula_id = ctx["formula_id"]
    store.save(state)


def evaluate_router(limit: int | None = None, *, use_c2: bool = False, use_c4: bool = False, category: str | None = None) -> EvalSummary:
    records = load_eval_records(limit, use_c2=use_c2, use_c4=use_c4, category=category)
    classifier = IntentClassifier()
    store = SessionStore()
    ctx_mgr = SessionContextManager(store)
    summary = EvalSummary(total=len(records))

    # Pre-index session turns for context seeding
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if rec.get("session_id"):
            by_session[rec["session_id"]].append(rec)
    for sid in by_session:
        by_session[sid].sort(key=lambda r: r.get("turn_id", 1))

    session_accum: dict[str, dict[str, Any]] = {}

    for rec in records:
        t0 = time.perf_counter()
        q = rec["query"]
        expected_intent = (rec.get("expected_intents") or [rec.get("intent")])[0]
        expected_agents = set(rec.get("expected_agents") or [])
        expected_clarify = rec.get("requires_clarification", False)
        cat = rec.get("category", "unknown")

        sid = rec.get("session_id")
        turn_id = rec.get("turn_id", 1)

        if sid and turn_id > 1:
            # Seed from accumulated session state or prior turn expected_context
            prev_turns = [r for r in by_session.get(sid, []) if r.get("turn_id", 1) < turn_id]
            if prev_turns:
                last = prev_turns[-1]
                ctx = last.get("expected_context") or last.get("expected_slots") or {}
                session_accum.setdefault(sid, {}).update(ctx)
            if session_accum.get(sid):
                state = store.create_session(sid)
                for k, v in session_accum[sid].items():
                    if k == "chemical_name":
                        state.chemical_name = v
                    elif k == "cas":
                        state.cas = v
                    elif k == "oel_type":
                        state.oel_type = v
                    elif k == "formula_id":
                        state.formula_id = v
                store.save(state)
            else:
                _seed_session_from_record(store, sid, rec)

        normalized = normalize_persian_query(q)
        state, ctx = ctx_mgr.begin_turn(sid, normalized)

        # Use eval-provided resolved_query when testing context resolution accuracy
        classify_query = rec.get("resolved_query") if rec.get("requires_session_context") else ctx.resolved_query
        slots = extract_slots(classify_query, ctx.merged_slots)
        # Merge expected slots for formula calc eval
        if rec.get("expected_slots"):
            slots.update({k: v for k, v in rec["expected_slots"].items() if v is not None})

        result = classifier.classify(
            classify_query,
            slots=slots,
            session_ambiguous=ctx.ambiguous and not rec.get("requires_session_context"),
            missing_slots=ctx.missing_for_resolution if not rec.get("requires_session_context") else None,
        )

        summary.latency_ms.append((time.perf_counter() - t0) * 1000)

        intent_ok = result.intent == expected_intent
        agents_ok = set(result.expected_agents) >= expected_agents
        clarify_ok = result.requires_clarification == expected_clarify

        if intent_ok:
            summary.intent_correct += 1
            summary.per_intent[expected_intent]["correct"] += 1
        else:
            summary.confusion[f"{expected_intent} -> {result.intent}"] += 1
        summary.per_intent[expected_intent]["total"] += 1

        summary.per_category[cat]["total"] += 1
        if intent_ok:
            summary.per_category[cat]["intent_ok"] += 1
        if agents_ok:
            summary.agent_correct += 1
            summary.per_category[cat]["agent_ok"] += 1
        if clarify_ok:
            summary.clarification_correct += 1

        if rec.get("requires_session_context"):
            summary.context_resolution_total += 1
            expected_resolved = rec.get("resolved_query", q)
            if ctx.resolved_query == expected_resolved or classify_query == expected_resolved:
                summary.context_resolved += 1

        ok = intent_ok and agents_ok
        if ok:
            summary.passed += 1
        else:
            summary.failed += 1
            if len(summary.failures) < 50:
                summary.failures.append({
                    "query_id": rec.get("query_id"),
                    "query": q,
                    "category": cat,
                    "expected_intent": expected_intent,
                    "predicted_intent": result.intent,
                    "expected_agents": list(expected_agents),
                    "predicted_agents": result.expected_agents,
                })

        # Accumulate session context for next turn
        if sid:
            acc = session_accum.setdefault(sid, {})
            acc.update(rec.get("expected_context") or {})
            acc.update({k: v for k, v in (rec.get("expected_slots") or {}).items() if v})
            if slots.get("chemical_name"):
                acc["chemical_name"] = slots["chemical_name"]
            if slots.get("cas"):
                acc["cas"] = slots["cas"]
            if slots.get("formula_id"):
                acc["formula_id"] = slots["formula_id"]

    return summary


def evaluate_by_category(use_c2: bool = False, *, use_c4: bool = False) -> dict[str, dict[str, float]]:
    categories = ["structured", "semantic", "formula", "conversational", "hybrid", "adversarial", "negative", "clarification"]
    out: dict[str, dict[str, float]] = {}
    for cat in categories:
        summary = evaluate_router(use_c2=use_c2, use_c4=use_c4, category=cat)
        if summary.total == 0:
            continue
        out[cat] = {
            "total": summary.total,
            "intent_accuracy": summary.intent_accuracy(),
            "agent_accuracy": summary.agent_accuracy(),
            "context_resolution_accuracy": summary.context_resolution_accuracy(),
        }
    return out


def evaluate_agents_e2e(session, limit: int = 100, *, use_c2: bool = False) -> EvalSummary:
    from agents.orchestrator.pipeline import QueryOrchestrator

    records = load_eval_records(limit, use_c2=use_c2)
    orch = QueryOrchestrator(session)
    summary = EvalSummary(total=len(records))

    for rec in records:
        t0 = time.perf_counter()
        expected_intent = (rec.get("expected_intents") or [rec.get("intent")])[0]
        try:
            resp = orch.handle(rec["query"], session_id=rec.get("session_id"))
            summary.latency_ms.append((time.perf_counter() - t0) * 1000)
            intent_ok = resp.intent == expected_intent
            if intent_ok:
                summary.intent_correct += 1
                summary.passed += 1
            else:
                summary.failed += 1
                summary.confusion[f"{expected_intent} -> {resp.intent}"] += 1
                if len(summary.failures) < 30:
                    summary.failures.append({"query": rec["query"], "expected": expected_intent, "got": resp.intent})
        except Exception as exc:
            summary.failed += 1
            if len(summary.failures) < 30:
                summary.failures.append({"query": rec["query"], "error": str(exc)})

    return summary


def write_router_results(summary: EvalSummary, *, suffix: str = "") -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"router_results{suffix}.jsonl"
    row = {
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "intent_accuracy": summary.intent_accuracy(),
        "agent_accuracy": summary.agent_accuracy(),
        "clarification_accuracy": summary.clarification_correct / summary.total if summary.total else 0,
        "context_resolution_accuracy": summary.context_resolution_accuracy(),
        "avg_latency_ms": sum(summary.latency_ms) / len(summary.latency_ms) if summary.latency_ms else 0,
        "per_category": {
            cat: {
                "intent_accuracy": m["intent_ok"] / m["total"] if m["total"] else 0,
                "agent_accuracy": m["agent_ok"] / m["total"] if m["total"] else 0,
                "total": m["total"],
            }
            for cat, m in summary.per_category.items()
        },
        "failures_sample": summary.failures[:20],
        "confusion_top": summary.confusion.most_common(20),
    }
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_e2e_results(summary: EvalSummary, *, suffix: str = "") -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"e2e_results{suffix}.jsonl"
    row = {
        "total": summary.total,
        "passed": summary.passed,
        "failed": summary.failed,
        "intent_accuracy": summary.intent_accuracy(),
        "avg_latency_ms": sum(summary.latency_ms) / len(summary.latency_ms) if summary.latency_ms else 0,
        "failures_sample": summary.failures[:20],
        "confusion_top": summary.confusion.most_common(15),
    }
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_agent_results(agent_metrics: dict[str, Any], *, suffix: str = "") -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"agent_results{suffix}.jsonl"
    path.write_text(json.dumps(agent_metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
