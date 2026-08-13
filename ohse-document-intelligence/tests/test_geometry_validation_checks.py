"""Tests for promotion validation classification helpers."""

from __future__ import annotations

from document_ai.geometry_validation_checks import (
    is_multiline_first_line_anchor,
    is_numeric_wrong_row_match,
    should_flag_row_y_mismatch,
)

P46_ROW4_TEXT = (
    "کبد چرب؛ اختالل در رشد\n"
    "عصبی ؛ اختالل سیستم اعصاب\n"
    "مرکزی و سیستم ایمنی؛ آسیب\n"
    "سیستم تولید مثل مردان؛"
)


def test_p46_row4_multiline_first_line_not_flagged_as_row_y_mismatch():
    assert is_multiline_first_line_anchor(P46_ROW4_TEXT, 11.47)
    assert not should_flag_row_y_mismatch(
        cell_text=P46_ROW4_TEXT,
        bbox_y_center=233.8,
        expected_row_y=253.5,
        bbox_height=11.47,
    )


def test_single_line_row_y_mismatch_still_flagged():
    assert should_flag_row_y_mismatch(
        cell_text="10 ppm",
        bbox_y_center=129.2,
        expected_row_y=250.0,
        bbox_height=11.0,
    )


def test_multiline_tall_union_still_flagged_as_row_y_mismatch():
    assert not is_multiline_first_line_anchor("line1\nline2", 30.0)
    assert should_flag_row_y_mismatch(
        cell_text="line1\nline2",
        bbox_y_center=129.2,
        expected_row_y=250.0,
        bbox_height=30.0,
    )


def test_numeric_wrong_row_match():
    assert is_numeric_wrong_row_match(
        normalized_text="10",
        bbox_y_center=129.0,
        expected_row_y=250.0,
        persist=True,
    )
    assert not is_numeric_wrong_row_match(
        normalized_text="10",
        bbox_y_center=248.0,
        expected_row_y=250.0,
        persist=True,
    )
