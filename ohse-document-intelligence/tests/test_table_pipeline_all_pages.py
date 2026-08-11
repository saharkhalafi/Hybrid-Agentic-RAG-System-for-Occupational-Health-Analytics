"""Regression tests for all-pages table pipeline reporting helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_table_pipeline_all_pages import (
    _failure_clusters,
    _review_reason,
    aggregate_summary,
    worst_tables,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "docs" / "reports" / "table_pipeline_all_pages.json"


def _sample_diag(*, gold_allowed: bool = False, page: int = 99) -> dict:
    return {
        "page_number": page,
        "table_id": f"table_{page:03d}_01",
        "table_family": "chemical_oel",
        "physical_column_count": 7,
        "physical_row_count": 9,
        "recovery_method": "pymupdf_word_grid",
        "structural_confidence": 0.85,
        "gold_allowed": gold_allowed,
        "review_required": not gold_allowed,
        "mega_cell_count": 0,
        "numeric_validation": [],
        "table_quality": {
            "geometry_valid": gold_allowed,
            "header_structure_valid": gold_allowed,
            "merged_cells_valid": gold_allowed,
            "numeric_integrity_valid": gold_allowed,
            "provenance_complete": gold_allowed,
            "issues": [] if gold_allowed else [{"type": "merged_cell_unresolved"}],
        },
        "reconstructed_cells": [],
    }


def test_review_reason_when_gate_fails():
    diag = _sample_diag(gold_allowed=False)
    reason = _review_reason(diag)
    assert "header_reconstruction" in reason or "merged_cells" in reason or "provenance" in reason


def test_failure_clusters_merged_cells():
    diag = _sample_diag(gold_allowed=False)
    clusters = _failure_clusters(diag)
    assert "merged_cells" in clusters


def test_worst_tables_excludes_gold_allowed():
    diags = [_sample_diag(gold_allowed=True, page=50), _sample_diag(gold_allowed=False, page=71)]
    worst = worst_tables(diags, limit=20)
    assert worst
    assert worst[0]["page"] == 71
    assert all(item["page"] != 50 for item in worst)


def test_aggregate_summary_rates():
    diags = [_sample_diag(gold_allowed=True, page=46), _sample_diag(gold_allowed=False, page=99)]
    summary = aggregate_summary(
        pdf_path=Path("OHE6.pdf"),
        total_pdf_pages=389,
        table_pages=[46, 99],
        diagnostics=diags,
    )
    assert summary["total_tables"] == 2
    assert summary["tables_passed"] == 1
    assert summary["tables_review_required"] == 1
    assert summary["promote_gold"] is False


@pytest.mark.skipif(not REPORT_PATH.exists(), reason="full-document report not generated yet")
def test_all_pages_report_schema():
    payload = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert summary["total_tables"] == summary["table_pages"]
    assert summary["promote_gold"] is True
    assert summary.get("gold_tables", 0) + summary.get("tables_review_required", 0) == summary["total_tables"]
    assert 0 < summary.get("gold_tables", 0) <= summary["total_tables"]
    for key in (
        "gold_allowed_rate",
        "seven_column_reconstruction_rate",
        "numeric_integrity_rate",
        "provenance_coverage",
    ):
        assert key in summary
    assert len(payload["inventory"]) == summary["table_pages"]
    assert len(payload["tables"]) == summary["total_tables"]
