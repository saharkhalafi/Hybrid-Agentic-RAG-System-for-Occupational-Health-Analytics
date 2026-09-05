"""Formula Agent — registry lookup + deterministic calculation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Formula
from knowledge.calculation import evaluate_formula_by_id

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u0600-\u06FF]+")


@dataclass
class FormulaAgentResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "citations": self.citations,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


class FormulaAgent:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_formula(self, formula_id: str) -> dict[str, Any] | None:
        f = self.session.scalar(
            select(Formula).where(
                Formula.stable_formula_id == formula_id,
                Formula.validation_status == "accepted",
            )
        )
        if not f:
            return None
        return {
            "formula_id": f.stable_formula_id,
            "normalized_expression": f.normalized_expression,
            "original_expression": f.original_expression,
            "variables": f.variables,
            "page_number": f.page_number,
            "description": f.description,
            "domain": f.domain,
        }

    def search_formulas(
        self,
        query: str,
        *,
        formula_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Rank accepted formulas by stored text overlap. Does not invent expressions."""
        if formula_id:
            exact = self.get_formula(str(formula_id))
            if exact:
                return [exact]
        q_tokens = {t.lower() for t in _TOKEN_RE.findall(query or "") if len(t) > 1}
        if not q_tokens:
            return []
        rows = self.session.scalars(
            select(Formula).where(Formula.validation_status == "accepted")
        ).all()
        scored: list[tuple[int, dict[str, Any]]] = []
        for f in rows:
            blob = " ".join(
                str(p)
                for p in (
                    f.stable_formula_id,
                    f.original_expression,
                    f.normalized_expression,
                    f.description,
                    f.domain,
                    f.formula_name,
                    f.persian_name,
                )
                if p
            ).lower()
            tokens = {t for t in _TOKEN_RE.findall(blob) if len(t) > 1}
            overlap = len(q_tokens & tokens)
            if overlap <= 0:
                continue
            scored.append(
                (
                    overlap,
                    {
                        "formula_id": f.stable_formula_id,
                        "normalized_expression": f.normalized_expression,
                        "original_expression": f.original_expression,
                        "variables": f.variables,
                        "page_number": f.page_number,
                        "description": f.description,
                        "domain": f.domain,
                    },
                )
            )
        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def execute(
        self,
        intent: str,
        slots: dict[str, Any],
        *,
        query: str = "",
    ) -> FormulaAgentResult:
        import time

        t0 = time.perf_counter()
        try:
            if intent == "FORMULA.CALCULATION.VIBRATION_AHV":
                return self._calculate(slots, t0)
            if intent in {"FORMULA.LOOKUP.BY_ID", "FORMULA.LOOKUP.BY_DOMAIN", "FORMULA.VARIABLE.EXPLANATION"}:
                fid = slots.get("formula_id")
                meta = None
                if fid:
                    meta = self.get_formula(str(fid))
                if not meta:
                    hits = self.search_formulas(query or str(slots.get("domain") or ""), limit=1)
                    meta = hits[0] if hits else None
                if not meta:
                    return FormulaAgentResult(success=False, error="formula_not_found", latency_ms=_ms(t0))
                return FormulaAgentResult(
                    success=True,
                    data=meta,
                    citations=[
                        {
                            "formula_id": meta.get("formula_id"),
                            "page_number": meta.get("page_number"),
                            "authority": "postgresql",
                        }
                    ],
                    latency_ms=_ms(t0),
                )
            return FormulaAgentResult(success=False, error=f"unsupported_formula_intent:{intent}", latency_ms=_ms(t0))
        except Exception as exc:
            return FormulaAgentResult(success=False, error=str(exc), latency_ms=_ms(t0))

    def _calculate(self, slots: dict[str, Any], t0: float) -> FormulaAgentResult:
        fid = slots.get("formula_id") or "formula_240_01"
        meta = self.get_formula(fid)
        if not meta:
            return FormulaAgentResult(success=False, error="formula_not_found", latency_ms=_ms(t0))
        inputs = slots.get("variables") or {}
        if not inputs:
            return FormulaAgentResult(success=False, error="missing_inputs", latency_ms=_ms(t0))
        calc = evaluate_formula_by_id(fid, meta["normalized_expression"] or "", inputs)
        if not calc.valid:
            return FormulaAgentResult(
                success=False,
                error=";".join(calc.issues) or "calculation_failed",
                data=calc.to_dict(),
                latency_ms=_ms(t0),
            )
        return FormulaAgentResult(
            success=True,
            data=calc.to_dict(),
            citations=[{"formula_id": fid, "authority": "formula_engine"}],
            latency_ms=_ms(t0),
        )


def _ms(t0: float) -> float:
    import time

    return (time.perf_counter() - t0) * 1000
