"""Deterministic formula evaluation — LLM is never the calculator."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class CalculationResult:
    formula_id: str
    result: float
    inputs: dict[str, float]
    expression: str
    valid: bool
    issues: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula_id": self.formula_id,
            "result": self.result,
            "inputs": self.inputs,
            "expression": self.expression,
            "valid": self.valid,
            "issues": self.issues,
        }


def evaluate_vibration_daily_exposure(
    *,
    formula_id: str,
    ahw_values: list[float],
    t_values: list[float],
) -> CalculationResult:
    """Evaluate ahv = sqrt((1/T) * sum(ahw_i^2 * t_i)) deterministically."""
    issues: list[str] = []
    if len(ahw_values) != len(t_values):
        issues.append("ahw_and_t_length_mismatch")
        return CalculationResult(formula_id, float("nan"), {}, "ahv_rms_exposure", False, issues)

    if not t_values:
        issues.append("empty_exposure_intervals")
        return CalculationResult(formula_id, float("nan"), {}, "ahv_rms_exposure", False, issues)

    t_total = sum(t_values)
    if t_total <= 0:
        issues.append("non_positive_total_time")
        return CalculationResult(formula_id, float("nan"), {}, "ahv_rms_exposure", False, issues)

    summation = sum((ahw ** 2) * t for ahw, t in zip(ahw_values, t_values, strict=True))
    result = math.sqrt((1.0 / t_total) * summation)
    inputs = {f"ahw_{i+1}": v for i, v in enumerate(ahw_values)}
    inputs.update({f"t_{i+1}": v for i, v in enumerate(t_values)})
    inputs["T"] = t_total
    return CalculationResult(
        formula_id=formula_id,
        result=result,
        inputs=inputs,
        expression="ahv = sqrt((1/T) * sum(ahw_i^2 * t_i))",
        valid=True,
        issues=issues,
    )


def evaluate_formula_by_id(
    formula_id: str,
    normalized_expression: str,
    inputs: dict[str, float],
) -> CalculationResult:
    """Route to deterministic evaluator by formula pattern."""
    expr = normalized_expression.lower().replace(" ", "")
    if "ahv" in expr and "sqrt" in expr and "ahw" in expr:
        n = len([k for k in inputs if k.startswith("ahw_")])
        ahw_values = [inputs[f"ahw_{i}"] for i in range(1, n + 1) if f"ahw_{i}" in inputs]
        t_values = [inputs[f"t_{i}"] for i in range(1, n + 1) if f"t_{i}" in inputs]
        return evaluate_vibration_daily_exposure(
            formula_id=formula_id,
            ahw_values=ahw_values,
            t_values=t_values,
        )

    issues = [f"no_deterministic_evaluator_for:{formula_id}"]
    return CalculationResult(formula_id, float("nan"), inputs, normalized_expression, False, issues)


def parse_normalized_expression(expression: str) -> str:
    return re.sub(r"\s+", " ", expression.strip())
