"""Phase C.2 — Conversational evaluation dataset builder (300+ sessions, 1000+ turns)."""

from __future__ import annotations

import random
from typing import Any

from knowledge.calculation import evaluate_vibration_daily_exposure
from retrieval.eval_builders import BUILDER_VERSION, QueryCounter, _display_name
from retrieval.eval_gold_loader import GoldCorpus, OelRowRecord, numeric_from_field
from retrieval.eval_schema import (
    GroundTruth,
    RelevanceItem,
    RelevanceLevel,
    RetrievalEvalRecord,
    ReviewStatus,
    SourceTraceability,
)

BUILDER_VERSION_C2 = "2.0.0-conversational"
TARGET_SESSIONS = 300
TARGET_TURNS_MIN = 1000

# Turn templates by chain type
OEL_CHAIN_TEMPLATES: list[list[tuple[str, str, str, dict[str, Any]]]] = [
    # (query, resolved_query_template, intent, extra_slots)
    [
        ("حد مجاز {name} چقدره؟", "حد مجاز {name} چقدره؟", "STRUCTURED.OEL.ALL_LIMITS_LOOKUP", {}),
        ("STEL چی؟", "حد STEL {name} چقدر است؟", "STRUCTURED.OEL.STEL_LOOKUP", {"oel_type": "STEL"}),
        ("برای ۱۵ دقیقه چطور؟", "مواجهه ۱۵ دقیقه‌ای مجاز {name}", "STRUCTURED.OEL.STEL_LOOKUP", {"oel_type": "STEL", "duration": "15min"}),
        ("TWAش چنده؟", "TWA {name} چنده؟", "STRUCTURED.OEL.TWA_LOOKUP", {"oel_type": "TWA"}),
        ("پس کدومش بیشتره؟", "مقایسه TWA و STEL {name}", "HYBRID.LOOKUP_COMPARE_EXPLAIN", {}),
    ],
    [
        ("{name} چقدره؟", "حد TWA {name} چقدره؟", "STRUCTURED.OEL.TWA_LOOKUP", {"oel_type": "TWA"}),
        ("STEL نداره؟", "آیا {name} STEL دارد؟", "STRUCTURED.OEL.STEL_LOOKUP", {"oel_type": "STEL"}),
        ("CASش؟", "CAS {name} چیست؟", "STRUCTURED.CHEMICAL.BY_NAME", {}),
    ],
    [
        ("حد TWA {name}؟", "حد TWA {name} چقدر است؟", "STRUCTURED.OEL.TWA_LOOKUP", {"oel_type": "TWA"}),
        ("اگر مواجهه من ۲ ppm باشه چی؟", "مواجهه ۲ ppm {name} — مقایسه با حد مجاز", "HYBRID.LOOKUP_COMPARE_EXPLAIN", {"concentration": 2, "unit": "ppm"}),
        ("پس بیشتر از حد مجازه؟", "آیا ۲ ppm برای {name} بیشتر از حد مجاز است؟", "HYBRID.LOOKUP_COMPARE_EXPLAIN", {"concentration": 2}),
    ],
    [
        ("TWA {name} چنده؟", "TWA {name} چنده؟", "STRUCTURED.OEL.TWA_LOOKUP", {"oel_type": "TWA"}),
        ("TWA یعنی چی؟", "TWA یعنی چی؟", "SEMANTIC.DEFINITION.TWA", {}),
        ("برای {name} چه معنایی داره؟", "معنای TWA برای {name}", "HYBRID.LOOKUP_AND_EXPLAIN", {}),
    ],
    [
        ("حد {name}؟", "حد مجاز {name}؟", "CLARIFY.MISSING_LIMIT_TYPE", {}),
        ("TWA", "TWA {name} چنده؟", "STRUCTURED.OEL.TWA_LOOKUP", {"oel_type": "TWA"}),
    ],
]

TOPIC_SWITCH_TEMPLATES = [
    ("نه، منظورم {other} بود", "حد TWA {other} چقدر است؟", "STRUCTURED.OEL.TWA_LOOKUP"),
    ("بذار {other}", "حد مجاز {other}", "STRUCTURED.OEL.ALL_LIMITS_LOOKUP"),
]

FORMULA_CHAIN_TEMPLATES: list[list[tuple[str, str, str, dict[str, Any]]]] = [
    [
        ("برای محاسبه ahv چه چیزهایی لازمه؟", "پارامترهای فرمول ahv", "FORMULA.VARIABLE.EXPLANATION", {"formula_id": "formula_240_01"}),
        ("ahw1 من 2.5 و t1 برابر 4", "ورودی ahv: ahw1=2.5, t1=4", "FORMULA.CALCULATION.VIBRATION_AHV", {"variables": {"ahw_1": 2.5, "t_1": 4.0}}),
        ("ahw2=3 و t2=2 هم هست", "ahv با ahw1=2.5,t1=4,ahw2=3,t2=2", "FORMULA.CALCULATION.VIBRATION_AHV", {}),
        ("نتیجه چنده؟", "محاسبه ahv با ورودی‌های قبلی", "FORMULA.CALCULATION.VIBRATION_AHV", {}),
        ("یعنی چی؟", "تفسیر نتیجه ahv", "FORMULA.INTERPRETATION", {}),
    ],
    [
        ("فرمول formula_240_01 چیه؟", "فرمول formula_240_01", "FORMULA.LOOKUP.BY_ID", {"formula_id": "formula_240_01"}),
        ("با ahw1=1.5,t1=3 محاسبه کن", "ahv ahw1=1.5 t1=3", "FORMULA.CALCULATION.VIBRATION_AHV", {"variables": {"ahw_1": 1.5, "t_1": 3.0}}),
    ],
]

AMBIGUOUS_TEMPLATES = [
    ("حدش چنده؟", None, "CLARIFY.MISSING_CHEMICAL", {}),
    ("STEL؟", None, "CLARIFY.MISSING_CHEMICAL", {}),
    ("بیشتره؟", None, "CLARIFY.MISSING_EXPOSURE_VALUE", {}),
]

COLLOQUIAL_VARIANTS = {
    "چقدره؟": ["چنده؟", "چقدره؟", "چند ppm؟", "مقدارش؟"],
    "STEL": ["STEL", "استل", "حد کوتاه مدت", "۱۵ دقیقه‌ای"],
}


def _row_context(row: OelRowRecord) -> dict[str, Any]:
    name = _display_name(row)
    ctx: dict[str, Any] = {"chemical_name": name, "cas": row.cas}
    if row.twa:
        num = numeric_from_field(row.twa)
        if num and num.normalized_value is not None:
            ctx["twa_value"] = num.normalized_value
    return ctx


def _make_turn(
    counter: QueryCounter,
    *,
    session_id: str,
    turn_id: int,
    previous_ids: list[str],
    query: str,
    resolved: str | None,
    intent: str,
    row: OelRowRecord | None,
    inherited: dict[str, Any],
    requires_context: bool,
    requires_clarification: bool = False,
    agents: list[str] | None = None,
    tags: list[str] | None = None,
) -> RetrievalEvalRecord:
    agents = agents or (["structured"] if intent.startswith("STRUCTURED") else ["formula"] if intent.startswith("FORMULA") else ["semantic"])
    if intent.startswith("HYBRID"):
        agents = ["structured", "semantic"] if "COMPARE" not in intent else ["structured", "formula", "semantic"]
    if intent.startswith("CLARIFY"):
        agents = ["clarify"]
    if intent.startswith("GUARDRAIL"):
        agents = ["guardrail"]

    slots = dict(inherited)
    if row:
        slots.setdefault("chemical_name", _display_name(row))
        slots.setdefault("cas", row.cas)

    return RetrievalEvalRecord(
        query_id=counter.next_id("CONV"),
        session_id=session_id,
        turn_id=turn_id,
        previous_turn_ids=list(previous_ids),
        query=query,
        standalone_query=query,
        resolved_query=resolved or query,
        intent=intent,
        domain=intent.split(".")[0],
        category="conversational",
        difficulty="medium" if turn_id <= 2 else "hard",
        expected_agents=agents,
        requires_context=requires_context,
        requires_session_context=requires_context,
        requires_clarification=requires_clarification,
        slots=slots,
        expected_slots=dict(slots),
        inherited_slots={k: v for k, v in inherited.items() if k not in slots or slots[k] == v},
        expected_context=dict(inherited),
        tags=tags or ["conversation", f"turn{turn_id}"],
        review_status=ReviewStatus.MACHINE_VALIDATED,
        source_traceability=SourceTraceability(
            gold_artifact_path=row.gold_path if row else "gold/formulas/formula_240_01.json",
            builder_version=BUILDER_VERSION_C2,
        ),
    )


def _build_oel_sessions(
    corpus: GoldCorpus,
    counter: QueryCounter,
    *,
    max_sessions: int,
) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    candidates = [r for r in corpus.oel_rows if r.english_name and (r.twa or r.stel)][:max_sessions * 2]
    random.seed(42)
    random.shuffle(candidates)

    session_num = 1
    for row in candidates:
        if session_num > max_sessions:
            break
        name = _display_name(row)
        sid = f"CONV-{session_num:04d}"
        session_num += 1

        chain_idx = session_num % len(OEL_CHAIN_TEMPLATES)
        chain = OEL_CHAIN_TEMPLATES[chain_idx]
        # Vary chain length: 2-5 turns
        chain_len = 2 + (session_num % 4)
        chain = chain[:chain_len]

        inherited: dict[str, Any] = _row_context(row)
        prev_ids: list[str] = []

        for turn_id, (q_tpl, r_tpl, intent, extra) in enumerate(chain, start=1):
            q = q_tpl.format(name=name)
            resolved = r_tpl.format(name=name) if r_tpl else None
            inherited.update(extra)
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=prev_ids,
                query=q,
                resolved=resolved,
                intent=intent,
                row=row,
                inherited=inherited,
                requires_context=turn_id > 1,
                requires_clarification=intent.startswith("CLARIFY"),
                tags=["conversation", "oel_chain", f"turn{turn_id}"],
            )
            records.append(rec)
            prev_ids.append(rec.query_id)

        # Topic switch session every 10th
        if session_num % 10 == 0 and len(candidates) > session_num:
            other_row = candidates[(session_num + 5) % len(candidates)]
            other_name = _display_name(other_row)
            turn_id = len(prev_ids) + 1
            q, r, intent = TOPIC_SWITCH_TEMPLATES[0]
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=prev_ids,
                query=q.format(other=other_name),
                resolved=r.format(other=other_name),
                intent=intent,
                row=other_row,
                inherited={"chemical_name": other_name, "cas": other_row.cas},
                requires_context=True,
                tags=["conversation", "topic_switch"],
            )
            records.append(rec)

    return records


def _build_formula_sessions(counter: QueryCounter, *, count: int = 40) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for i in range(count):
        sid = f"CONV-FORM-{i + 1:04d}"
        chain = FORMULA_CHAIN_TEMPLATES[i % len(FORMULA_CHAIN_TEMPLATES)]
        inherited: dict[str, Any] = {"formula_id": "formula_240_01"}
        prev_ids: list[str] = []

        for turn_id, (q, resolved, intent, extra) in enumerate(chain, start=1):
            inherited.update(extra)
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=prev_ids,
                query=q,
                resolved=resolved,
                intent=intent,
                row=None,
                inherited=inherited,
                requires_context=turn_id > 1,
                agents=["formula"] if not intent.startswith("HYBRID") else ["formula", "semantic"],
                tags=["conversation", "formula_chain"],
            )
            records.append(rec)
            prev_ids.append(rec.query_id)
    return records


def _build_ambiguous_sessions(counter: QueryCounter, *, count: int = 30) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for i in range(count):
        sid = f"CONV-AMB-{i + 1:04d}"
        for turn_id, (q, resolved, intent, extra) in enumerate(AMBIGUOUS_TEMPLATES, start=1):
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=[],
                query=q,
                resolved=resolved,
                intent=intent,
                row=None,
                inherited={},
                requires_context=False,
                requires_clarification=True,
                tags=["conversation", "ambiguous"],
            )
            records.append(rec)
    return records


def _build_mixed_language_sessions(
    corpus: GoldCorpus,
    counter: QueryCounter,
    *,
    count: int = 50,
) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    rows = [r for r in corpus.oel_rows if r.english_name][:count]
    for i, row in enumerate(rows):
        name = _display_name(row)
        sid = f"CONV-MIX-{i + 1:04d}"
        chains = [
            (f"TWA {name}?", f"TWA {name} چنده؟", "STRUCTURED.OEL.TWA_LOOKUP"),
            ("STEL?", f"STEL {name}?", "STRUCTURED.OEL.STEL_LOOKUP"),
            (f"what is MW of {name}?", f"وزن مولکولی {name}", "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT"),
        ]
        inherited = _row_context(row)
        prev_ids: list[str] = []
        for turn_id, (q, resolved, intent) in enumerate(chains, start=1):
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=prev_ids,
                query=q,
                resolved=resolved.format(name=name) if "{name}" in resolved else resolved,
                intent=intent,
                row=row,
                inherited=inherited,
                requires_context=turn_id > 1,
                tags=["conversation", "mixed_language"],
            )
            records.append(rec)
            prev_ids.append(rec.query_id)
    return records


def build_conversational_eval_dataset(
    corpus: GoldCorpus,
    *,
    counter: QueryCounter | None = None,
    target_sessions: int = TARGET_SESSIONS,
    target_turns_min: int = TARGET_TURNS_MIN,
) -> list[RetrievalEvalRecord]:
    """Build 300+ sessions with 1000+ conversational turns."""
    counter = counter or QueryCounter(start=20000)
    records: list[RetrievalEvalRecord] = []

    oel_sessions = int(target_sessions * 0.75)
    records.extend(_build_oel_sessions(corpus, counter, max_sessions=oel_sessions))
    records.extend(_build_formula_sessions(counter, count=40))
    records.extend(_build_ambiguous_sessions(counter, count=30))
    records.extend(_build_mixed_language_sessions(corpus, counter, count=50))

    # Pad OEL sessions until turn target met
    session_count = len({r.session_id for r in records if r.session_id})
    turn_count = len(records)

    extra_idx = 0
    while (session_count < target_sessions or turn_count < target_turns_min) and extra_idx < 500:
        rows = [r for r in corpus.oel_rows if r.english_name]
        if not rows:
            break
        row = rows[extra_idx % len(rows)]
        name = _display_name(row)
        sid = f"CONV-PAD-{extra_idx + 1:04d}"
        inherited = _row_context(row)
        prev_ids: list[str] = []

        for turn_id, q_tpl in enumerate(
            ["حد {name}؟", "TWA؟", "STEL؟", "CAS؟"],
            start=1,
        ):
            intents = [
                "STRUCTURED.OEL.ALL_LIMITS_LOOKUP",
                "STRUCTURED.OEL.TWA_LOOKUP",
                "STRUCTURED.OEL.STEL_LOOKUP",
                "STRUCTURED.CHEMICAL.BY_NAME",
            ]
            q = q_tpl.format(name=name)
            resolved = q if turn_id == 1 else f"{intents[turn_id-1]} {name}"
            rec = _make_turn(
                counter,
                session_id=sid,
                turn_id=turn_id,
                previous_ids=prev_ids,
                query=q,
                resolved=resolved,
                intent=intents[turn_id - 1],
                row=row,
                inherited=inherited,
                requires_context=turn_id > 1,
                tags=["conversation", "padded"],
            )
            records.append(rec)
            prev_ids.append(rec.query_id)

        session_count = len({r.session_id for r in records if r.session_id})
        turn_count = len(records)
        extra_idx += 1

    return records
