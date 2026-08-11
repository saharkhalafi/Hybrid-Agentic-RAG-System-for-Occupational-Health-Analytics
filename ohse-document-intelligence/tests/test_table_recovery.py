"""Unit tests for word-level table recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.table_recovery import (
    COLUMN_FIELDS,
    WordToken,
    _build_row_cells,
    _compose_cell_text,
    _detect_column_centroids,
    _filter_row_anchors,
    _kmeans_1d,
    _nearest_column,
    recover_chemical_oel_table,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = PROJECT_ROOT.parent / "OHE6.pdf"


def test_kmeans_1d_separates_clusters():
    values = [10, 12, 11, 100, 102, 200, 201, 300, 400, 500, 550]
    centroids = _kmeans_1d(values, 4)
    assert len(centroids) == 4
    assert centroids == sorted(centroids)


def test_compose_cell_text_rtl_line_order():
    words = [
        WordToken("Acetone", 400, 100, 440, 112, 1),
        WordToken("[67-64-1]", 350, 100, 395, 112, 1),
        WordToken("استون", 450, 100, 490, 112, 1),
    ]
    text = _compose_cell_text(words)
    assert "Acetone" in text
    assert "[67-64-1]" in text


def test_build_row_cells_splits_by_x():
    page_width = 612.0
    centroids = _detect_column_centroids(
        [
            WordToken("تحریک", 80, 120, 110, 132, 1),
            WordToken("20", 250, 120, 265, 132, 1),
            WordToken("ppm", 268, 120, 290, 132, 1),
            WordToken("85/10", 380, 120, 410, 132, 1),
            WordToken("Acetone", 430, 120, 470, 132, 1),
            WordToken("[67-64-1]", 400, 120, 428, 132, 1),
            WordToken("8", 580, 120, 590, 132, 1),
        ],
        page_width,
    )
    row_words = [
        WordToken("تحریک", 80, 120, 110, 132, 1),
        WordToken("20", 250, 120, 265, 132, 1),
        WordToken("ppm", 268, 120, 290, 132, 1),
        WordToken("85/10", 380, 120, 410, 132, 1),
        WordToken("Acetone", 430, 120, 470, 132, 1),
        WordToken("[67-64-1]", 400, 120, 428, 132, 1),
        WordToken("8", 580, 120, 590, 132, 1),
    ]
    cells = _build_row_cells(1, row_words, centroids, page_width)
    by_col = {c.column: c for c in cells}
    assert 6 in by_col
    assert by_col[6].text.strip() == "8"
    assert len(cells) >= 4
    for cell in cells:
        if cell.column in {2, 3, 4, 6}:
            assert cell.bbox is not None
            assert cell.bbox["width"] < 200


def test_filter_row_anchors_drops_limit_fragments():
    anchors = [
        (131.9, "8"),
        (207.9, "10"),
        (239.9, "2"),  # limit fragment — must drop
        (245.4, "11"),
        (303.6, "1"),  # limit fragment after row 12 — must drop
        (309.3, "13"),
    ]
    filtered = _filter_row_anchors(anchors)
    nums = [a[1] for a in filtered]
    assert nums == ["8", "10", "11", "13"]


@pytest.mark.skipif(not PDF_PATH.exists(), reason="OHE6.pdf not available")
def test_recover_page_47_eight_data_rows_including_aspirin():
    table = recover_chemical_oel_table(PDF_PATH, 47)
    assert table is not None
    data_rows = sorted({c.row for c in table.flat_cells() if c.row > 0})
    assert len(data_rows) == 8
    by_row = {}
    for cell in table.flat_cells():
        if cell.row > 0:
            by_row.setdefault(cell.row, {})[cell.column] = cell.text
    aspirin_rows = [
        r for r, cols in by_row.items() if "50-78-2" in cols.get(5, "") or "Acetylsalicylic" in cols.get(5, "")
    ]
    assert len(aspirin_rows) == 1
    aspirin = by_row[aspirin_rows[0]]
    assert aspirin.get(6, "").strip() in {"", "-", "—"}  # unnumbered row
    assert "180" in aspirin.get(4, "")
    assert "3/0" in aspirin.get(3, "") or "3/0" in aspirin.get(2, "")


@pytest.mark.skipif(not PDF_PATH.exists(), reason="OHE6.pdf not available")
def test_recover_page_47_acetylene_descriptive_limits_spatially_separate():
    table = recover_chemical_oel_table(PDF_PATH, 47)
    assert table is not None
    acetylene_row = None
    for cell in table.flat_cells():
        if cell.row > 0 and "Acetylene" in cell.text:
            acetylene_row = cell.row
            break
    assert acetylene_row is not None
    cols = {c.column: c.text for c in table.flat_cells() if c.row == acetylene_row}
    stel_text = cols.get(2, "")
    twa_text = cols.get(3, "")
    assert "خفگی" in twa_text
    assert "D" in stel_text or "آور" in stel_text
    assert stel_text != twa_text


@pytest.mark.skipif(not PDF_PATH.exists(), reason="OHE6.pdf not available")
def test_recover_page_47_no_mega_cells_in_limits():
    table = recover_chemical_oel_table(PDF_PATH, 47)
    assert table is not None
    data_cells = [c for c in table.flat_cells() if c.row > 0]
    limit_cells = [c for c in data_cells if c.column in {2, 3, 4}]
    assert limit_cells, "expected STEL/TWA/MW cells"
    for cell in limit_cells:
        assert cell.bbox is not None
        assert cell.bbox["width"] < 120, f"col {cell.column} mega-cell: {cell.text[:40]!r} w={cell.bbox['width']}"
    chem_cells = [c for c in data_cells if c.column == 5]
    for cell in chem_cells:
        assert cell.bbox is not None
        assert cell.bbox["width"] < 200


@pytest.mark.skipif(not PDF_PATH.exists(), reason="OHE6.pdf not available")
def test_recover_page_47_acetone_cyanohydrin_columns():
    table = recover_chemical_oel_table(PDF_PATH, 47)
    assert table is not None
    data_cells = [c for c in table.flat_cells() if c.row > 0]
    by_row_col = {(c.row, c.column): c for c in data_cells}
    # First data row (document row 8) — MW, STEL, TWA, chemical, row_number in separate columns.
    row_cells = [c for c in data_cells if "Acetone" in c.text or "cyanohydrin" in c.text]
    assert row_cells, "expected Acetone cyanohydrin row"
    row_idx = row_cells[0].row
    assert (row_idx, 4) in by_row_col
    assert "85" in by_row_col[(row_idx, 4)].text or "10" in by_row_col[(row_idx, 4)].text
    assert (row_idx, 5) in by_row_col
    assert "Acetone" in by_row_col[(row_idx, 5)].text
    assert (row_idx, 6) in by_row_col
    assert by_row_col[(row_idx, 6)].text.strip() in {"8", "۸"}
    assert "Acetone" not in by_row_col[(row_idx, 6)].text
    table = recover_chemical_oel_table(PDF_PATH, 46)
    assert table is not None
    cols = {c.column for c in table.flat_cells() if c.row > 0}
    assert max(cols) >= 5
    assert 6 in cols  # row_number column present
