"""Read-only geometry validation classification helpers.

These checks are used by promotion/forensic validation scripts only.
They do NOT alter GeometryResolver behavior.
"""

from __future__ import annotations

import re

ROW_Y_MISMATCH_THRESHOLD = 18.0
TALL_BBOX_THRESHOLD = 25.0

NUMERIC_ONLY_RE = re.compile(r"^[\d۰-۹٠-٩\s./,-]+$")


def is_multiline_first_line_anchor(
    cell_text: str,
    bbox_height: float | None,
    *,
    tall_threshold: float = TALL_BBOX_THRESHOLD,
) -> bool:
    """True when a multiline cell bbox covers a single line, not a cross-row union."""
    if not cell_text or "\n" not in cell_text:
        return False
    if bbox_height is None:
        return False
    return bbox_height <= tall_threshold


def should_flag_row_y_mismatch(
    *,
    cell_text: str,
    bbox_y_center: float,
    expected_row_y: float,
    bbox_height: float | None,
    row_y_threshold: float = ROW_Y_MISMATCH_THRESHOLD,
) -> bool:
    """Return True when an accepted cell should be flagged B_row_y_mismatch."""
    if abs(bbox_y_center - expected_row_y) <= row_y_threshold:
        return False
    if is_multiline_first_line_anchor(cell_text, bbox_height):
        return False
    return True


def is_numeric_wrong_row_match(
    *,
    normalized_text: str,
    bbox_y_center: float | None,
    expected_row_y: float | None,
    persist: bool,
    row_y_threshold: float = ROW_Y_MISMATCH_THRESHOLD,
) -> bool:
    """Accepted numeric-only cell whose bbox Y is far from row peers."""
    if not persist or bbox_y_center is None or expected_row_y is None:
        return False
    compact = normalized_text.replace(" ", "")
    if not compact or not NUMERIC_ONLY_RE.match(compact):
        return False
    return abs(bbox_y_center - expected_row_y) > row_y_threshold


def has_cross_row_bbox_union(bbox_height: float | None, *, tall_threshold: float = TALL_BBOX_THRESHOLD) -> bool:
    return bbox_height is not None and bbox_height > tall_threshold
