"""Deterministic session context resolution for Persian follow-up queries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agents.session.store import SessionState, SessionStore

FOLLOWUP_FRAGMENTS = re.compile(
    r"^(STEL|TWA|Ceiling|سقف|CAS|MW|منبع|فرمول|ahv|A\(8\)|"
    r"نداره\؟|چنده\؟|چقدره\؟|همون|اون|اش|ش\؟|پس |باز |دوباره |"
    r"TWA\؟|STEL\؟|CAS\؟|سقف\؟|حد\؟|)",
    re.IGNORECASE,
)
PRONOUN_MARKERS = ("همون", "اون", "اش", "ش ", "ش؟", "اون ماده", "همین")
TOPIC_SWITCH = re.compile(r"(تعریف|چیست|چیه|فرمول|BEI|صدا|ارتعاش|بیولوژ)", re.IGNORECASE)
CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
OEL_TYPE_MAP = {
    "twa": "TWA",
    "stel": "STEL",
    "ceiling": "CEILING",
    "سقف": "CEILING",
    "c": "CEILING",
}


@dataclass
class SessionContext:
    session_id: str | None
    turn_id: int
    raw_query: str
    resolved_query: str
    requires_context: bool
    inherited_slots: dict[str, Any] = field(default_factory=dict)
    explicit_slots: dict[str, Any] = field(default_factory=dict)
    merged_slots: dict[str, Any] = field(default_factory=dict)
    context_trace: list[str] = field(default_factory=list)
    ambiguous: bool = False
    missing_for_resolution: list[str] = field(default_factory=list)


class SessionContextManager:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()

    def begin_turn(self, session_id: str | None, raw_query: str) -> tuple[SessionState | None, SessionContext]:
        state = self.store.create_session(session_id) if session_id else None
        turn_id = (state.turn_id + 1) if state else 1
        ctx = SessionContext(
            session_id=session_id,
            turn_id=turn_id,
            raw_query=raw_query.strip(),
            resolved_query=raw_query.strip(),
            requires_context=False,
        )
        if not state or state.turn_id == 0:
            ctx.context_trace.append("no_prior_context")
            q = ctx.raw_query
            if len(q) < 25 and re.search(r"(حد|چقدر|چنده|مجاز|نداره)", q) and not self._has_explicit_entity(q):
                ctx.ambiguous = True
                ctx.missing_for_resolution.append("chemical_name")
                ctx.context_trace.append("ambiguous_no_session")
            return state, ctx

        return state, self._resolve(state, ctx)

    def _resolve(self, state: SessionState, ctx: SessionContext) -> SessionContext:
        q = ctx.raw_query
        trace = ctx.context_trace

        # explicit CAS overrides
        cas_match = CAS_PATTERN.search(q)
        if cas_match:
            ctx.explicit_slots["cas"] = cas_match.group(1)
            trace.append("explicit_cas")

        # explicit OEL type
        for key, oel in OEL_TYPE_MAP.items():
            if re.search(rf"\b{re.escape(key)}\b", q, re.IGNORECASE):
                ctx.explicit_slots["oel_type"] = oel
                trace.append(f"explicit_oel_type:{oel}")
                break

        # topic switch — do not inherit chemical for pure definition queries
        if TOPIC_SWITCH.search(q) and not state.chemical_name:
            trace.append("topic_switch_no_chemical_inherit")

        is_followup = (
            len(q) < 60
            and (
                any(p in q for p in PRONOUN_MARKERS)
                or FOLLOWUP_FRAGMENTS.match(q)
                or (state.chemical_name and not self._has_explicit_entity(q))
            )
        )

        if is_followup:
            ctx.requires_context = True
            trace.append("followup_detected")
            if state.chemical_name and "chemical_name" not in ctx.explicit_slots:
                ctx.inherited_slots["chemical_name"] = state.chemical_name
                trace.append(f"inherited_chemical:{state.chemical_name}")
            if state.cas and "cas" not in ctx.explicit_slots:
                ctx.inherited_slots["cas"] = state.cas
            if state.oel_type and "oel_type" not in ctx.explicit_slots:
                ctx.inherited_slots["oel_type"] = state.oel_type
            if state.formula_id and "formula_id" not in ctx.explicit_slots:
                ctx.inherited_slots["formula_id"] = state.formula_id
            if state.formula_variables and "variables" not in ctx.explicit_slots:
                ctx.inherited_slots["variables"] = dict(state.formula_variables)
            if state.exposure_value is not None and "concentration" not in ctx.explicit_slots:
                ctx.inherited_slots["concentration"] = state.exposure_value
                if state.exposure_unit:
                    ctx.inherited_slots["unit"] = state.exposure_unit

            ctx.resolved_query = self._expand_followup(q, state, ctx)
            trace.append(f"resolved:{ctx.resolved_query}")

        ctx.merged_slots = {**ctx.inherited_slots, **ctx.explicit_slots}

        if is_followup and not ctx.merged_slots.get("chemical_name") and not ctx.merged_slots.get("cas"):
            if re.search(r"(نداره|چنده|چقدر|بیشتر|مجاز)", q):
                ctx.ambiguous = True
                ctx.missing_for_resolution.append("chemical_name")
                trace.append("ambiguous_missing_chemical")

        return ctx

    @staticmethod
    def _has_explicit_entity(query: str) -> bool:
        if CAS_PATTERN.search(query):
            return True
        # Latin chemical-like token
        return bool(re.search(r"\b[A-Z][a-z]{2,}(?:\s+[a-z]+)?\b", query))

    @staticmethod
    def _expand_followup(query: str, state: SessionState, ctx: SessionContext) -> str:
        chem = ctx.merged_slots.get("chemical_name") or state.chemical_name or ""
        q = query.strip()
        q_lower = q.lower()
        if re.match(r"^STEL\s*\??$", q, re.I) or ("stel" in q_lower and len(q) < 15):
            return f"حد STEL {chem} چقدر است؟" if chem else f"آیا {chem} STEL دارد؟" if chem else query
        if re.match(r"^TWA\s*\??$", q, re.I) or (q_lower == "twa" or q_lower == "twa?"):
            return f"حد TWA {chem} چقدر است؟" if chem else query
        if re.match(r"^CAS\s*\??$", q, re.I) or "cas" in q_lower and len(q) < 12:
            return f"CAS {chem} چیست؟" if chem else query
        if "سقف" in q or "ceiling" in q_lower:
            return f"حد Ceiling {chem} چقدر است؟" if chem else query
        if "نداره" in q and "stel" in q_lower:
            return f"آیا {chem} STEL دارد؟" if chem else query
        if "بیشتر" in q and chem:
            conc = ctx.merged_slots.get("concentration") or state.exposure_value
            if conc is not None:
                return f"آیا {conc} ppm برای {chem} بیشتر از حد مجاز است؟"
        if state.formula_id and re.search(r"(نتیجه|چنده|محاسبه|اگر|زمان)", q):
            return f"محاسبه {state.formula_id} با ورودی‌های قبلی: {q}"
        if chem and len(q) < 30:
            return f"{q} (ماده: {chem})"
        return query

    def commit_turn(
        self,
        state: SessionState | None,
        *,
        ctx: SessionContext,
        intent: str,
        slots: dict[str, Any],
        trace_id: str,
        agent_results: dict[str, Any] | None = None,
    ) -> None:
        if not state:
            return
        state.turn_id = ctx.turn_id
        if slots.get("chemical_name"):
            state.chemical_name = slots["chemical_name"]
        if slots.get("cas"):
            state.cas = slots["cas"]
        if slots.get("oel_type"):
            state.oel_type = slots["oel_type"]
        if slots.get("formula_id"):
            state.formula_id = slots["formula_id"]
        if slots.get("variables"):
            state.formula_variables = dict(slots["variables"])
        if slots.get("concentration") is not None:
            state.exposure_value = float(slots["concentration"])
        if slots.get("unit"):
            state.exposure_unit = slots["unit"]
        state.previous_intent = intent
        state.previous_resolved_query = ctx.resolved_query
        state.previous_query_id = trace_id
        if agent_results:
            state.agent_results = agent_results
        state.history.append({
            "turn_id": ctx.turn_id,
            "raw_query": ctx.raw_query,
            "resolved_query": ctx.resolved_query,
            "intent": intent,
            "slots": slots,
            "trace_id": trace_id,
        })
        self.store.save(state)
