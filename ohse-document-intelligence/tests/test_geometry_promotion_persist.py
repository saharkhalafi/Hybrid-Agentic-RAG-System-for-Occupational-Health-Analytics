"""Dry-run persistence plan tests."""

from __future__ import annotations

from persistence.geometry_promotion_persist import build_cell_evidence_plan, stable_cell_idempotency_key


def test_stable_cell_idempotency_key_format():
    key = stable_cell_idempotency_key("hash", 46, "table_046_01", 4, 0)
    assert key == "hash:46:table_046_01:4:0"


def test_build_cell_evidence_plan_covers_dispositions():
    sample = [
        {
            "cell_id": "cell_t_1_0",
            "table_id": "t",
            "page_number": 46,
            "row_index": 1,
            "column_index": 0,
            "cell_text": "x",
            "disposition": "accept",
            "persist": True,
            "rejection_reason": "none",
            "match_method": "exact",
        },
        {
            "cell_id": "cell_t_1_1",
            "table_id": "t",
            "page_number": 46,
            "row_index": 1,
            "column_index": 1,
            "cell_text": "y",
            "disposition": "review",
            "persist": True,
            "rejection_reason": "none",
            "review_flags": ["A_multi_search_for"],
            "match_method": "exact",
        },
        {
            "cell_id": "cell_t_1_2",
            "table_id": "t",
            "page_number": 46,
            "row_index": 1,
            "column_index": 2,
            "cell_text": "",
            "disposition": "reject",
            "persist": False,
            "rejection_reason": "empty_cell",
            "match_method": "none",
        },
    ]
    planned = build_cell_evidence_plan(
        {"cells": sample, "generated_at": "t"},
        content_hash="abc",
        vs_cells={},
        vs_tables={},
    )
    assert {p.disposition for p in planned} == {"accept", "review", "reject"}
    assert all(not p.missing_required for p in planned)
