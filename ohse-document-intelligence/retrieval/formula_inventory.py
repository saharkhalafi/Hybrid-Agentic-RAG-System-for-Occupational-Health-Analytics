"""Formula registry inventory — executable vs registry-only vs unsupported."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Formula
from knowledge.calculation import evaluate_formula_by_id

EXECUTABLE_FORMULAS = frozenset({"formula_240_01"})


@dataclass
class FormulaInventoryEntry:
    formula_id: str
    status: str  # executable | registry_only | unsupported
    domain: str | None
    page_number: int | None
    has_variables: bool
    normalized_expression: str | None
    notes: str = ""


def inventory_formulas(session: Session) -> list[FormulaInventoryEntry]:
    rows = session.scalars(
        select(Formula).where(Formula.validation_status == "accepted")
    ).all()
    entries: list[FormulaInventoryEntry] = []
    for f in rows:
        fid = f.stable_formula_id
        if fid in EXECUTABLE_FORMULAS:
            status = "executable"
            notes = "deterministic evaluator available"
        elif f.normalized_expression and "ahv" in (f.normalized_expression or "").lower():
            status = "registry_only"
            notes = "ahv pattern but no deterministic evaluator registered"
        else:
            status = "unsupported"
            notes = "incomplete expression or no evaluator"
        entries.append(
            FormulaInventoryEntry(
                formula_id=fid,
                status=status,
                domain=f.domain,
                page_number=f.page_number,
                has_variables=bool(f.variables),
                normalized_expression=f.normalized_expression,
                notes=notes,
            )
        )
    return entries


def classify_formula(formula_id: str, session: Session) -> FormulaInventoryEntry | None:
    for e in inventory_formulas(session):
        if e.formula_id == formula_id:
            return e
    return None


def is_executable(formula_id: str) -> bool:
    return formula_id in EXECUTABLE_FORMULAS
