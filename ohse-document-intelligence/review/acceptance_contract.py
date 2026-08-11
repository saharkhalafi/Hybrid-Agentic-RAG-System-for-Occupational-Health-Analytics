"""Deterministic acceptance policy — hard validation failures override confidence."""

from __future__ import annotations

from typing import Any

CONTRACT_DIMENSIONS = (
    "numeric_integrity",
    "cell_reference_integrity",
    "bbox_integrity",
    "table_structure",
    "cas_validation",
    "unit_validation",
    "semantic_validation",
)


def evaluate_acceptance_contract(validation: dict[str, str]) -> dict[str, Any]:
    """Return contract results and whether all dimensions passed."""
    contract: dict[str, str] = {}
    all_passed = True
    for dim in CONTRACT_DIMENSIONS:
        status = validation.get(dim, "review_required")
        contract[dim] = status
        if status != "passed":
            all_passed = False
    return {
        "contract": contract,
        "passed": all_passed,
        "overall_status": "passed" if all_passed else "review_required",
    }


def contract_passed(acceptance_contract: dict[str, str] | None) -> bool:
    if not acceptance_contract:
        return False
    return all(acceptance_contract.get(dim) == "passed" for dim in CONTRACT_DIMENSIONS)
