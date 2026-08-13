"""Detect and split logical table rows spanning multiple visual Y bands."""

from __future__ import annotations

import statistics
from typing import Any

from goldset_generator.document_processor import ExtractedCellRecord, _cell_id

# Match geometry resolver row tolerance — do not widen globally.
VISUAL_Y_BAND_TOLERANCE = 18.0
MIN_BAND_SPLIT_SPREAD = 36.0


def _y_center(bbox: dict[str, Any] | None) -> float | None:
    if not bbox:
        return None
    height = float(bbox.get("height") or 0)
    if height <= 0:
        return None
    return float(bbox["y"]) + height / 2.0


def cluster_y_centers(y_centers: list[float], *, tolerance: float = VISUAL_Y_BAND_TOLERANCE) -> list[list[float]]:
    if not y_centers:
        return []
    sorted_ys = sorted(y_centers)
    clusters: list[list[float]] = [[sorted_ys[0]]]
    for y in sorted_ys[1:]:
        cluster_mean = statistics.mean(clusters[-1])
        if abs(y - cluster_mean) <= tolerance:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    return clusters


def row_visual_band_spread(row: list[ExtractedCellRecord]) -> float | None:
    ys = [_y_center(cell.bbox) for cell in row if _y_center(cell.bbox) is not None]
    if len(ys) < 2:
        return None
    return max(ys) - min(ys)


def row_has_multi_visual_band(row: list[ExtractedCellRecord]) -> bool:
    ys = [_y_center(cell.bbox) for cell in row if _y_center(cell.bbox) is not None]
    if len(ys) < 2:
        return False
    clusters = cluster_y_centers(ys)
    if len(clusters) <= 1:
        return False
    return (max(ys) - min(ys)) >= MIN_BAND_SPLIT_SPREAD


def _assign_cell_to_band(cell: ExtractedCellRecord, band_centers: list[float]) -> int:
    yc = _y_center(cell.bbox)
    if yc is not None:
        return min(range(len(band_centers)), key=lambda i: abs(yc - band_centers[i]))
    # No bbox: attach to band of nearest populated column in same row.
    return 0 if band_centers else 0


def split_row_by_visual_bands(row: list[ExtractedCellRecord]) -> list[list[ExtractedCellRecord]]:
    """Split one logical row into multiple rows when cells span distinct visual Y bands."""
    ys = [_y_center(cell.bbox) for cell in row if _y_center(cell.bbox) is not None]
    if len(ys) < 2:
        return [row]

    clusters = cluster_y_centers(ys)
    if len(clusters) <= 1:
        return [row]
    if max(ys) - min(ys) < MIN_BAND_SPLIT_SPREAD:
        return [row]

    band_centers = [statistics.mean(cluster) for cluster in clusters]
    result: list[list[ExtractedCellRecord]] = []

    for band_idx, _band_center in enumerate(band_centers):
        built: list[ExtractedCellRecord] = []
        for cell in row:
            assigned_band = _assign_cell_to_band(cell, band_centers)
            if assigned_band == band_idx:
                built.append(cell)
            elif not (cell.text or "").strip() and not is_valid_cell_bbox(cell):
                built.append(cell)
            else:
                built.append(
                    ExtractedCellRecord(
                        cell_id=cell.cell_id,
                        table_id=cell.table_id,
                        page_number=cell.page_number,
                        row=cell.row,
                        column=cell.column,
                        text="",
                        bbox=None,
                        confidence=cell.confidence,
                        bbox_confidence=None,
                        bbox_source=None,
                        source=cell.source,
                        normalized_value=None,
                        source_reference={
                            **(cell.source_reference or {}),
                            "visual_band_placeholder": True,
                            "source_row_index": cell.row,
                        },
                    )
                )
        if any((c.text or "").strip() or is_valid_cell_bbox(c) for c in built):
            result.append(built)
    return result if result else [row]


def is_valid_cell_bbox(cell: ExtractedCellRecord) -> bool:
    bbox = cell.bbox
    if not bbox:
        return False
    return float(bbox.get("height") or 0) > 0 and float(bbox.get("width") or 0) > 0


def refine_table_visual_rows(
    table_id: str,
    rows: list[list[ExtractedCellRecord]],
) -> tuple[list[list[ExtractedCellRecord]], int]:
    """Split body rows with multi-visual-band layout. Returns (new_rows, split_count)."""
    if not rows:
        return rows, 0

    header, *body = rows
    new_body: list[list[ExtractedCellRecord]] = []
    split_count = 0

    for row in body:
        if row_has_multi_visual_band(row):
            parts = split_row_by_visual_bands(row)
            if len(parts) > 1:
                split_count += 1
            new_body.extend(parts)
        else:
            new_body.append(row)

    new_rows = [header, *new_body]
    for row_index, row in enumerate(new_rows):
        for cell in row:
            cell.row = row_index
            cell.cell_id = _cell_id(table_id, row_index, cell.column)
            cell.source_reference = {
                **(cell.source_reference or {}),
                "visual_row_index": row_index,
            }
    return new_rows, split_count
