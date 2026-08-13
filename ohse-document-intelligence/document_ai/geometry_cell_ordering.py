"""Deterministic cell processing order for row-aware geometry resolution."""

from __future__ import annotations

OEL_LIMIT_COLUMNS = frozenset({1, 2, 3})
OEL_NAME_COLUMN = 5
OEL_ROW_NUMBER_COLUMN = 6


def geometry_resolve_sort_key(
    *,
    page_number: int,
    table_id: str,
    row_index: int,
    column_index: int,
) -> tuple[int, str, int, int, int]:
    """Resolve limit/anchor columns before name and row-number columns."""
    if column_index in OEL_LIMIT_COLUMNS:
        priority = column_index
    elif column_index == 4:
        priority = 10
    elif column_index == 0:
        priority = 11
    elif column_index == OEL_NAME_COLUMN:
        priority = 12
    elif column_index == OEL_ROW_NUMBER_COLUMN:
        priority = 99
    else:
        priority = 50
    return (page_number, table_id, row_index, priority, column_index)
