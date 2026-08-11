"""Structural confidence scoring when OCR confidence is unavailable."""

from __future__ import annotations

from document_ai.schema import ExtractionRow


def compute_structural_confidence(rows: list[ExtractionRow]) -> float:
    if not rows:
        return 0.0

    column_counts = [len(row.cells) for row in rows if row.cells]
    if not column_counts:
        return 0.0

    dominant_columns = max(set(column_counts), key=column_counts.count)
    consistent_rows = sum(1 for count in column_counts if count == dominant_columns)
    row_consistency = consistent_rows / len(column_counts)

    non_empty_cells = 0
    total_cells = 0
    for row in rows:
        for cell in row.cells:
            total_cells += 1
            if cell.text.strip():
                non_empty_cells += 1

    completeness = non_empty_cells / total_cells if total_cells else 0.0
    return round(min((row_consistency * 0.55) + (completeness * 0.45), 1.0), 4)
