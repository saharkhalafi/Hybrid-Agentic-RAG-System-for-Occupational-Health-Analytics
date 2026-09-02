"""Recover chemical OEL tables from digital PDF pages using word-level PyMuPDF geometry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from ingestion.pdf_geometry import PageGeometryIndex, union_word_bbox
from normalization.persian_normalizer import normalize_persian_text

CAS_PATTERN = re.compile(r"\[\d{2,7}-\d{2}-\d\]")
ROW_NUM_TOKEN = re.compile(r"^[\d۰-۹]{1,3}$")
PERSIAN_DIGIT = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Descriptive limit phrases that may span STEL/TWA columns (simple asphyxiant, oxygen deficiency, etc.)
DESCRIPTIVE_LIMIT_MARKERS = (
    "خفگی",
    "asphyx",
    "simple",
    "oxygen",
    "اکسیژن",
    "D)",
    "(D",
    "محتوی",
)

NUM_COLUMNS = 7

# Boundary ratios between adjacent columns (derived from page 50 Document AI geometry).
# Physical x-order RTL: C0=exposure_basis … C6=row_number.
_COLUMN_BOUNDARY_RATIOS = (0.232, 0.332, 0.417, 0.511, 0.604, 0.845)

# Row-number tokens appear in the rightmost ~15% of the page.
_ROW_NUMBER_MIN_X_RATIO = 0.85

# Document AI physical order: C0=left … C6=row_number (right)
COLUMN_FIELDS = (
    "health_effect",
    "symbols",
    "STEL",
    "TWA",
    "molecular_weight",
    "chemical_name",
    "row_number",
)

# Max cell width (pt) before flagging a geometry failure per column class.
_MAX_CELL_WIDTH = {
    0: 120.0,  # exposure_basis — may wrap
    1: 60.0,
    2: 80.0,
    3: 80.0,
    4: 50.0,
    5: 180.0,  # chemical name — widest allowed
    6: 40.0,
}


@dataclass(frozen=True)
class WordToken:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page_number: int

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class RecoveredCell:
    row: int
    column: int
    column_name: str
    text: str
    bbox: dict[str, float] | None
    confidence: float = 0.9
    source_words: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "column": self.column,
            "column_name": self.column_name,
            "text": self.text,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "source_words": self.source_words,
        }


@dataclass
class RecoveredTable:
    page_number: int
    recovery_method: str = "pymupdf_word_grid"
    cells: list[RecoveredCell] = field(default_factory=list)
    rows: list[list[RecoveredCell]] = field(default_factory=list)
    raw_markdown: str = ""
    table_type: str = "chemical_oel"
    structural_confidence: float = 0.75
    bbox: dict[str, float] | None = None
    column_centroids: list[float] = field(default_factory=list)
    word_assignments: list[dict[str, Any]] = field(default_factory=list)

    def flat_cells(self) -> list[RecoveredCell]:
        return [cell for row in self.rows for cell in row]


def _column_name(col_idx: int) -> str:
    if 0 <= col_idx < len(COLUMN_FIELDS):
        return COLUMN_FIELDS[col_idx]
    return f"column_{col_idx}"


def _extract_word_tokens(page: fitz.Page, page_number: int) -> list[WordToken]:
    tokens: list[WordToken] = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("words"):
        cleaned = text.strip()
        if not cleaned:
            continue
        tokens.append(WordToken(text=cleaned, x0=x0, y0=y0, x1=x1, y1=y1, page_number=page_number))
    return tokens


def _kmeans_1d(values: list[float], k: int, *, max_iter: int = 40) -> list[float]:
    if not values:
        return []
    if len(values) <= k:
        return sorted(set(values))

    sorted_vals = sorted(values)
    step = max(1, len(sorted_vals) // k)
    centroids = [sorted_vals[min(len(sorted_vals) - 1, i * step)] for i in range(k)]

    for _ in range(max_iter):
        buckets: list[list[float]] = [[] for _ in range(k)]
        for value in values:
            idx = min(range(k), key=lambda i: abs(value - centroids[i]))
            buckets[idx].append(value)
        new_centroids: list[float] = []
        for idx, bucket in enumerate(buckets):
            if bucket:
                new_centroids.append(sum(bucket) / len(bucket))
            else:
                new_centroids.append(centroids[idx])
        if all(abs(a - b) < 0.25 for a, b in zip(new_centroids, centroids)):
            centroids = new_centroids
            break
        centroids = new_centroids
    return sorted(centroids)


def _nearest_column(x_center: float, centroids: list[float]) -> int:
    return min(range(len(centroids)), key=lambda i: abs(x_center - centroids[i]))


def _find_row_anchors(words: list[WordToken], page_width: float) -> list[tuple[float, str]]:
    min_x = page_width * _ROW_NUMBER_MIN_X_RATIO
    anchors: list[tuple[float, str]] = []
    for word in words:
        if word.x_center < min_x:
            continue
        token = word.text.strip().translate(PERSIAN_DIGIT)
        if not ROW_NUM_TOKEN.match(token):
            continue
        anchors.append((word.y_center, token))
    anchors.sort(key=lambda item: item[0])
    deduped: list[tuple[float, str]] = []
    for y, text in anchors:
        if deduped and abs(y - deduped[-1][0]) < 6:
            continue
        deduped.append((y, text))
    return _filter_row_anchors(deduped)


def _filter_row_anchors(anchors: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Drop limit-value digits mis-detected as row numbers in the ردیف column."""
    if not anchors:
        return []
    filtered: list[tuple[float, str]] = [anchors[0]]
    for y, text in anchors[1:]:
        try:
            prev_num = int(filtered[-1][1])
            curr_num = int(text)
        except ValueError:
            continue
        # Limit fragments (0/1, small digits) after established row sequence.
        if prev_num >= 8 and curr_num <= 3:
            continue
        # Non-monotonic drops (e.g. row 10 → 2 → 11).
        if curr_num < prev_num - 1:
            continue
        # Same-band duplicate anchor.
        if abs(y - filtered[-1][0]) < 12:
            continue
        filtered.append((y, text))
    return filtered


def _row_y_bounds(
    anchors: list[tuple[float, str]],
    header_bottom: float,
    *,
    words: list[WordToken] | None = None,
) -> list[tuple[float, float, int]]:
    """Return (y0, y1, row_index) bands — row_index is 1-based data row slot."""
    if not anchors:
        return []
    bounds: list[tuple[float, float, int]] = []
    for idx, (y, _text) in enumerate(anchors):
        y0 = header_bottom if idx == 0 else (anchors[idx - 1][0] + y) / 2
        y1 = (y + anchors[idx + 1][0]) / 2 if idx + 1 < len(anchors) else y + max(45, 30)
        bounds.append((y0, y1, idx + 1))
    return bounds


def _assign_row_by_band(
    word: WordToken,
    bands: list[tuple[float, float, int]],
    header_bottom: float,
) -> int | None:
    if word.y_center < header_bottom:
        return 0
    if not bands:
        return None
    for y0, y1, row_idx in bands:
        if y0 <= word.y_center <= y1:
            return row_idx
    # Fallback: nearest band by center distance (handles edge overlap).
    best = min(bands, key=lambda b: min(abs(word.y_center - b[0]), abs(word.y_center - b[1])))
    y0, y1, row_idx = best
    margin = 18.0
    if word.y_center >= y0 - margin and word.y_center <= y1 + margin:
        return row_idx
    return None


def _chemical_column_range(page_width: float) -> tuple[float, float]:
    bounds = _column_boundaries(page_width)
    left = bounds[4] if len(bounds) > 4 else page_width * 0.604
    return left, page_width


def _cluster_words_by_y(words: list[WordToken], *, tolerance: float = 14.0) -> list[list[WordToken]]:
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: w.y_center)
    clusters: list[list[WordToken]] = [[sorted_words[0]]]
    for word in sorted_words[1:]:
        if abs(word.y_center - clusters[-1][0].y_center) <= tolerance:
            clusters[-1].append(word)
        else:
            clusters.append([word])
    return clusters


def _detect_orphan_cas_anchors(
    words: list[WordToken],
    anchors: list[tuple[float, str]],
    header_bottom: float,
    page_width: float,
) -> list[tuple[float, str]]:
    """Find data rows with CAS/chemical evidence but no ردیف anchor (e.g. aspirin on p47)."""
    if not anchors:
        return []
    chem_left, _chem_right = _chemical_column_range(page_width)
    bounds = _column_boundaries(page_width)
    stel_left = bounds[1] if len(bounds) > 1 else page_width * 0.232
    mw_right = bounds[4] if len(bounds) > 4 else page_width * 0.604

    body_words = [w for w in words if w.y_center >= header_bottom]
    bands = _row_y_bounds(anchors, header_bottom)

    # Words already well-covered by anchor bands (near anchor y).
    anchor_ys = {a[0] for a in anchors}

    candidate_words: list[WordToken] = []
    for word in body_words:
        if chem_left <= word.x_center <= page_width * 0.98:
            candidate_words.append(word)
        elif CAS_PATTERN.search(word.text):
            candidate_words.append(word)
        elif stel_left <= word.x_center <= mw_right and any(m in word.text for m in DESCRIPTIVE_LIMIT_MARKERS):
            candidate_words.append(word)

    orphans: list[tuple[float, str]] = []
    for cluster in _cluster_words_by_y(candidate_words):
        if len(cluster) < 2 and not any(CAS_PATTERN.search(w.text) for w in cluster):
            continue
        y_center = sum(w.y_center for w in cluster) / len(cluster)
        # Skip if cluster sits on an existing anchor row.
        if any(abs(y_center - ay) < 20 for ay in anchor_ys):
            continue
        # Skip if cluster is already inside a band tied to an anchor within 18px.
        if any(abs(y_center - ay) < 18 for ay in anchor_ys):
            continue
        cluster_text = " ".join(w.text for w in cluster)
        if not (CAS_PATTERN.search(cluster_text) or any(m in cluster_text for m in ("acid", "Acid", "اسید", "آسپ"))):
            if not any(w.x_center >= chem_left for w in cluster):
                continue
        # Must sit in a gap between consecutive anchors (unnumbered insert row).
        inserted = False
        for idx in range(len(anchors) - 1):
            y_prev, _ = anchors[idx]
            y_next, _ = anchors[idx + 1]
            if y_prev + 15 < y_center < y_next - 15:
                orphans.append((y_center, ""))
                inserted = True
                break
        if not inserted and anchors and y_center > anchors[-1][0] + 25:
            # Trailing orphan below last anchor — rare; skip to avoid noise.
            pass
    # Deduplicate orphan y positions.
    orphans.sort(key=lambda item: item[0])
    deduped: list[tuple[float, str]] = []
    for y, text in orphans:
        if deduped and abs(y - deduped[-1][0]) < 18:
            continue
        deduped.append((y, text))
    return deduped


def _merge_anchor_lists(
    primary: list[tuple[float, str]],
    orphans: list[tuple[float, str]],
) -> list[tuple[float, str]]:
    merged = sorted(primary + orphans, key=lambda item: item[0])
    deduped: list[tuple[float, str]] = []
    for y, text in merged:
        if deduped and abs(y - deduped[-1][0]) < 15:
            if text and not deduped[-1][1]:
                deduped[-1] = (y, text)
            continue
        deduped.append((y, text))
    return deduped


def _clean_header_leaked_row_numbers(cells: list[RecoveredCell]) -> None:
    """Remove printed page numbers leaked into header chemical column (e.g. '46\\nنام علمی')."""
    for cell in cells:
        if cell.row != 0 or cell.column != 5:
            continue
        lines = cell.text.split("\n")
        cleaned_lines = [ln for ln in lines if not ROW_NUM_TOKEN.match(ln.strip().translate(PERSIAN_DIGIT))]
        if cleaned_lines and len(cleaned_lines) != len(lines):
            cell.text = "\n".join(cleaned_lines).strip()


def _compose_cell_text(words: list[WordToken]) -> str:
    if not words:
        return ""
    by_line: dict[int, list[WordToken]] = {}
    for word in words:
        y_key = round(word.y0 / 3) * 3
        by_line.setdefault(y_key, []).append(word)
    lines: list[str] = []
    for y_key in sorted(by_line):
        line_words = sorted(by_line[y_key], key=lambda w: -w.x0)
        lines.append(" ".join(w.text for w in line_words))
    return "\n".join(lines).strip()


def _word_bbox_dict(words: list[WordToken]) -> dict[str, float] | None:
    if not words:
        return None
    return {
        "x": min(w.x0 for w in words),
        "y": min(w.y0 for w in words),
        "width": max(w.x1 for w in words) - min(w.x0 for w in words),
        "height": max(w.y1 for w in words) - min(w.y0 for w in words),
    }


def _word_provenance(words: list[WordToken]) -> list[dict[str, Any]]:
    return [
        {
            "page": w.page_number,
            "word": w.text,
            "bbox": {"x": w.x0, "y": w.y0, "width": w.x1 - w.x0, "height": w.y1 - w.y0},
            "source_span": f"{w.x0:.1f},{w.y0:.1f}-{w.x1:.1f},{w.y1:.1f}",
        }
        for w in words
    ]


def _is_header_word(word: WordToken) -> bool:
    markers = ("ردیف", "TWA", "STEL", "نام علمی", "ملکولی", "نماد", "مبنای", "وزن")
    return any(m in word.text for m in markers)


def _column_boundaries(page_width: float) -> list[float]:
    return [page_width * ratio for ratio in _COLUMN_BOUNDARY_RATIOS]


def _assign_column_by_boundary(x_center: float, page_width: float) -> int:
    bounds = _column_boundaries(page_width)
    for col in range(NUM_COLUMNS):
        left = 0.0 if col == 0 else bounds[col - 1]
        right = page_width if col == NUM_COLUMNS - 1 else bounds[col]
        if left <= x_center < right:
            return col
    return NUM_COLUMNS - 1


def _detect_column_centroids(words: list[WordToken], page_width: float) -> list[float]:
    """Return column center x-positions from the fixed OEL template."""
    bounds = _column_boundaries(page_width)
    centers: list[float] = []
    prev = 0.0
    for boundary in bounds:
        centers.append((prev + boundary) / 2)
        prev = boundary
    centers.append((bounds[-1] + page_width) / 2)
    return centers


def _assign_column(word: WordToken, centroids: list[float], page_width: float) -> int:
    """Geometry-only column assignment via fixed template boundaries."""
    _ = centroids  # kept for API compatibility / diagnostics
    return _assign_column_by_boundary(word.x_center, page_width)


def recover_stel_twa_from_words(
    words: list[WordToken],
    page_width: float,
    y_min: float,
    y_max: float,
) -> dict[str, str | None]:
    """Assign STEL/TWA by physical x-band, not Document AI text order.

    Column 2 is STEL/C (left of the pair); column 3 is TWA. Used when DAI
    cloned one merged-limit bbox onto both logical cells.
    """
    buckets: dict[int, list[WordToken]] = {2: [], 3: []}
    for word in words:
        if not (y_min <= word.y_center <= y_max):
            continue
        if _is_header_word(word):
            continue
        col = _assign_column_by_boundary(word.x_center, page_width)
        if col in buckets:
            buckets[col].append(word)
    return {
        "STEL": _compose_cell_text(buckets[2]) or None,
        "TWA": _compose_cell_text(buckets[3]) or None,
    }


def _assign_row(
    word: WordToken,
    anchors: list[tuple[float, str]],
    header_bottom: float,
    *,
    max_distance: float = 28.0,
) -> int | None:
    """Legacy wrapper — prefer band assignment via merged anchors."""
    bands = _row_y_bounds(anchors, header_bottom)
    return _assign_row_by_band(word, bands, header_bottom)


def _build_cells_from_grid(
    grid: dict[tuple[int, int], list[WordToken]],
) -> list[RecoveredCell]:
    cells: list[RecoveredCell] = []
    for (row_index, col), col_words in sorted(grid.items()):
        if not col_words:
            continue
        text = _compose_cell_text(col_words)
        if not text or text in {"-", "—", "–"}:
            continue
        bbox = _word_bbox_dict(col_words)
        cells.append(
            RecoveredCell(
                row=row_index,
                column=col,
                column_name=_column_name(col),
                text=text,
                bbox=bbox,
                confidence=0.92,
                source_words=_word_provenance(col_words),
            )
        )
    return cells


def _build_row_cells(
    row_index: int,
    words: list[WordToken],
    centroids: list[float],
    page_width: float,
) -> list[RecoveredCell]:
    grid: dict[tuple[int, int], list[WordToken]] = {}
    for word in words:
        col = _assign_column(word, centroids, page_width)
        grid.setdefault((row_index, col), []).append(word)
    return _build_cells_from_grid(grid)


def _build_header_cells(words: list[WordToken], centroids: list[float], page_width: float, header_bottom: float) -> list[RecoveredCell]:
    header_words = [w for w in words if w.y0 < header_bottom]
    return _build_row_cells(0, header_words, centroids, page_width)


def _mega_cell_violations(cells: list[RecoveredCell]) -> int:
    violations = 0
    for cell in cells:
        if not cell.bbox or cell.row == 0:
            continue
        limit = _MAX_CELL_WIDTH.get(cell.column, 100.0)
        if cell.bbox["width"] > limit:
            violations += 1
    return violations


def recover_chemical_oel_table(pdf_path: Path, page_number: int) -> RecoveredTable | None:
    """Recover a chemical OEL table using word-level x/y grid reconstruction."""
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_number - 1)
        page_width = page.rect.width
        words = _extract_word_tokens(page, page_number)

        if not words:
            return None

        centroids = _detect_column_centroids(words, page_width)
        raw_anchors = _find_row_anchors(words, page_width)
        if len(raw_anchors) < 2:
            geom = PageGeometryIndex(page)
            if not any(CAS_PATTERN.search(line.text) for line in geom.lines):
                return None
            return _recover_from_word_lines(page_number, geom)

        header_bottom = max(raw_anchors[0][0] - 25, 95)
        orphan_anchors = _detect_orphan_cas_anchors(words, raw_anchors, header_bottom, page_width)
        anchors = _merge_anchor_lists(raw_anchors, orphan_anchors)
        bands = _row_y_bounds(anchors, header_bottom)

        header_cells = _build_header_cells(words, centroids, page_width, header_bottom)
        _clean_header_leaked_row_numbers(header_cells)

        grid: dict[tuple[int, int], list[WordToken]] = {}
        assignments: list[dict[str, Any]] = []

        for word in words:
            row_id = _assign_row_by_band(word, bands, header_bottom)
            if row_id is None:
                continue
            col_id = _assign_column(word, centroids, page_width)
            grid.setdefault((row_id, col_id), []).append(word)
            assignments.append(
                {
                    "page": page_number,
                    "word": word.text,
                    "bbox": {
                        "x": word.x0,
                        "y": word.y0,
                        "width": word.x1 - word.x0,
                        "height": word.y1 - word.y0,
                    },
                    "row_id": row_id,
                    "column_id": col_id,
                    "source_span": f"{word.x0:.1f},{word.y0:.1f}-{word.x1:.1f},{word.y1:.1f}",
                }
            )

        row_indices = sorted({row for row, _col in grid if row > 0})

        all_cells = _build_cells_from_grid(grid)
        data_rows: list[list[RecoveredCell]] = []
        table_bboxes: list[dict[str, float]] = []

        for row_index in row_indices:
            row_cells = [c for c in all_cells if c.row == row_index]
            if row_cells:
                data_rows.append(row_cells)
                for cell in row_cells:
                    if cell.bbox:
                        table_bboxes.append(cell.bbox)

        if not data_rows:
            return None

        all_rows = [header_cells] + data_rows
        flat = [c for row in all_rows for c in row]
        mega_violations = _mega_cell_violations(flat)
        base_conf = 0.70 + min(0.22, 0.025 * len(data_rows))
        structural_confidence = max(0.5, base_conf - 0.04 * mega_violations)

        table_bbox = None
        if table_bboxes:
            table_bbox = {
                "x": min(b["x"] for b in table_bboxes),
                "y": min(b["y"] for b in table_bboxes),
                "width": max(b["x"] + b["width"] for b in table_bboxes) - min(b["x"] for b in table_bboxes),
                "height": max(b["y"] + b["height"] for b in table_bboxes) - min(b["y"] for b in table_bboxes),
            }

        markdown_lines = [
            " | ".join((cell.text or "").replace("\n", " ") for cell in row) for row in all_rows
        ]

        return RecoveredTable(
            page_number=page_number,
            recovery_method="pymupdf_word_grid",
            cells=flat,
            rows=all_rows,
            raw_markdown="\n".join(markdown_lines),
            table_type="chemical_oel",
            structural_confidence=structural_confidence,
            bbox=table_bbox,
            column_centroids=centroids,
            word_assignments=assignments,
        )
    finally:
        doc.close()


def _recover_from_word_lines(page_number: int, geom: PageGeometryIndex) -> RecoveredTable | None:
    cas_lines = [line for line in geom.lines if CAS_PATTERN.search(line.text)]
    if not cas_lines:
        return None

    data_rows: list[list[RecoveredCell]] = []
    for row_idx, line in enumerate(cas_lines, start=1):
        words = list(line.words) if line.words else []
        bbox = (
            union_word_bbox(list(words))
            if words
            else {
                "x": line.bbox[0],
                "y": line.bbox[1],
                "width": line.bbox[2] - line.bbox[0],
                "height": line.bbox[3] - line.bbox[1],
            }
        )
        data_rows.append(
            [
                RecoveredCell(
                    row=row_idx,
                    column=5,
                    column_name="chemical_name",
                    text=line.text,
                    bbox=bbox,
                    confidence=0.75,
                    source_words=_word_provenance(
                        [
                            WordToken(w.text, w.x0, w.y0, w.x1, w.y1, page_number)
                            for w in words
                        ]
                    ),
                )
            ]
        )

    header = [
        RecoveredCell(0, idx, name, name, None, 0.8) for idx, name in enumerate(COLUMN_FIELDS)
    ]
    all_rows = [header] + data_rows
    return RecoveredTable(
        page_number=page_number,
        recovery_method="pymupdf_word_grid_fallback",
        cells=[c for row in all_rows for c in row],
        rows=all_rows,
        raw_markdown="\n".join(line.text for line in cas_lines),
        table_type="chemical_oel",
        structural_confidence=0.65,
    )


def recover_tables_for_page(pdf_path: Path, page_number: int, page_text: str) -> list[RecoveredTable]:
    from goldset_generator.table_detection_gate import has_chemical_oel_signatures

    if not has_chemical_oel_signatures(page_text):
        return []
    table = recover_chemical_oel_table(pdf_path, page_number)
    return [table] if table else []


def recovered_table_to_dict(table: RecoveredTable) -> dict[str, Any]:
    return {
        "page_number": table.page_number,
        "recovery_method": table.recovery_method,
        "table_type": table.table_type,
        "structural_confidence": table.structural_confidence,
        "raw_markdown": table.raw_markdown,
        "bbox": table.bbox,
        "column_centroids": table.column_centroids,
        "word_assignments": table.word_assignments,
        "cells": [cell.to_dict() for cell in table.flat_cells()],
        "rows": [[cell.to_dict() for cell in row] for row in table.rows],
    }
