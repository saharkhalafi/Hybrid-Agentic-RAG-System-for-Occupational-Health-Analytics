from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


# ============================================================================
# PATHS
# ============================================================================

LAYER2_PATH = Path(
    "data/intermediate/validated_structure_46-55.json"
)

GOLD_PATH = Path(
    "tests/fixtures/oel_gold_46_55.json"
)


# ============================================================================
# HELPERS
# ============================================================================

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path.resolve()}"
        )

    return json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )


def cell_text(cell: dict[str, Any]) -> str:
    """
    Extract cell text from Layer 2 cell structure.

    Layer 2 structure:

        table["rows"] -> list[list[cell]]

    Each cell is already a normal dict containing:
        cell["text"]
    """

    if not isinstance(cell, dict):
        return ""

    return str(
        cell.get("text") or ""
    ).strip()


def row_text(row: list[dict[str, Any]]) -> list[str]:
    return [
        cell_text(cell)
        for cell in row
    ]


def print_separator(char: str = "=", width: int = 100):
    print(char * width)


def print_row(
    row_index: int,
    row: list[dict[str, Any]],
    prefix: str = "",
):
    print()
    print(
        f"{prefix}ROW {row_index}"
    )
    print(
        f"{prefix}COLUMN COUNT: {len(row)}"
    )

    for cell in row:
        column = cell.get(
            "column"
        )

        text = cell_text(cell)

        cell_id = cell.get(
            "cell_id"
        )

        source_row = cell.get(
            "row"
        )

        print(
            f"{prefix}  COL {column}: "
            f"{text!r}"
            f" | cell_id={cell_id}"
            f" | source_row={source_row}"
        )


def print_table_summary(
    table: dict[str, Any],
):
    print_separator()

    print(
        "TABLE:",
        table.get("table_id"),
    )

    print(
        "PAGE :",
        table.get("page_number"),
    )

    print(
        "TYPE :",
        table.get("table_type"),
    )

    rows = table.get(
        "rows"
    ) or []

    print(
        "ROWS :",
        len(rows),
    )

    if rows:
        print(
            "COLS :",
            len(rows[0]),
        )


# ============================================================================
# RAW ROW CONVERSION
# ============================================================================

def layer2_to_raw_rows(
    table: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Convert Layer 2 rows into the raw structure expected by:

        reconstruct_logical_rows()

    IMPORTANT:

    Layer 2:

        rows = [
            [
                cell,
                cell,
                ...
            ],
            ...
        ]

    Reconstructor expects:

        rows = [
            {
                "cells": [
                    {
                        "blocks": [
                            {
                                "textBlock": {
                                    "text": "..."
                                }
                            }
                        ]
                    }
                ]
            }
        ]
    """

    raw_rows: list[
        dict[str, Any]
    ] = []

    for row in table.get("rows") or []:

        raw_cells: list[
            dict[str, Any]
        ] = []

        for cell in row:

            text = cell_text(
                cell
            )

            blocks = []

            if text:
                blocks.append(
                    {
                        "textBlock": {
                            "text": text
                        }
                    }
                )

            raw_cells.append(
                {
                    "blocks": blocks,

                    "rowSpan": cell.get(
                        "row_span",
                        1,
                    ),

                    "columnSpan": cell.get(
                        "column_span",
                        1,
                    ),
                }
            )

        raw_rows.append(
            {
                "cells": raw_cells
            }
        )

    return raw_rows


# ============================================================================
# RECONSTRUCTED ROW TEXT
# ============================================================================

def raw_cell_text(
    cell: dict[str, Any],
) -> str:

    texts = []

    for block in (
        cell.get("blocks") or []
    ):

        text_block = (
            block.get(
                "textBlock"
            )
            or {}
        )

        text = text_block.get(
            "text",
            "",
        )

        if text:
            texts.append(
                str(text)
            )

    return " ".join(
        texts
    ).strip()


def reconstructed_row_text(
    row: dict[str, Any],
) -> list[str]:

    return [
        raw_cell_text(cell)
        for cell in (
            row.get("cells")
            or []
        )
    ]


# ============================================================================
# CHEMICAL SEARCH
# ============================================================================

def contains_any(
    text: str,
    terms: list[str],
) -> bool:

    text_lower = text.lower()

    return any(
        term.lower()
        in text_lower
        for term in terms
    )


def row_contains_chemical(
    row: list[dict[str, Any]],
    terms: list[str],
) -> bool:

    return any(
        contains_any(
            cell_text(cell),
            terms,
        )
        for cell in row
    )


# ============================================================================
# FIND RECONSTRUCTION CANDIDATES
# ============================================================================

def find_reconstruction_candidates(
    tables: list[dict[str, Any]],
):
    """
    Run the real reconstruction function on every table and report
    tables where:

        len(output) < len(input)

    These are the tables where visual rows were actually merged.
    """

    from document_ai.logical_row_reconstructor import (
        reconstruct_logical_rows,
    )

    candidates = []

    print_separator()

    print(
        "SCANNING ALL TABLES FOR LOGICAL ROW RECONSTRUCTION"
    )

    print_separator()

    for table in tables:

        table_id = table.get(
            "table_id"
        )

        page = table.get(
            "page_number"
        )

        rows = table.get(
            "rows"
        ) or []

        raw_rows = (
            layer2_to_raw_rows(
                table
            )
        )

        reconstructed = (
            reconstruct_logical_rows(
                raw_rows
            )
        )

        source_count = len(
            raw_rows
        )

        output_count = len(
            reconstructed
        )

        if output_count < source_count:

            candidates.append(
                {
                    "table": table,
                    "raw_rows": raw_rows,
                    "reconstructed": reconstructed,
                    "source_count": source_count,
                    "output_count": output_count,
                }
            )

            print()
            print(
                f"[FOUND] "
                f"page={page} "
                f"table={table_id} "
                f"{source_count} -> "
                f"{output_count}"
            )

    print()

    print(
        "TOTAL RECONSTRUCTION CANDIDATES:",
        len(candidates),
    )

    return candidates


# ============================================================================
# PRINT RECONSTRUCTION DETAILS
# ============================================================================

def print_reconstruction_details(
    candidate: dict[str, Any],
):

    table = candidate[
        "table"
    ]

    raw_rows = candidate[
        "raw_rows"
    ]

    reconstructed = candidate[
        "reconstructed"
    ]

    print_separator()

    print(
        "RECONSTRUCTION DETAIL"
    )

    print_separator()

    print(
        "TABLE:",
        table.get("table_id"),
    )

    print(
        "PAGE:",
        table.get("page_number"),
    )

    print(
        "INPUT ROWS:",
        len(raw_rows),
    )

    print(
        "OUTPUT ROWS:",
        len(reconstructed),
    )

    # ------------------------------------------------------------------
    # SOURCE
    # ------------------------------------------------------------------

    print()
    print_separator("-")

    print(
        "SOURCE VISUAL ROWS"
    )

    print_separator("-")

    for i, row in enumerate(
        raw_rows
    ):

        print()
        print(
            f"SOURCE ROW {i}"
        )

        values = []

        for j, cell in enumerate(
            row.get("cells") or []
        ):

            values.append(
                (
                    j,
                    raw_cell_text(cell)
                )
            )

        for column, text in values:

            print(
                f"  COL {column}: "
                f"{text!r}"
            )

    # ------------------------------------------------------------------
    # RECONSTRUCTED
    # ------------------------------------------------------------------

    print()
    print_separator("-")

    print(
        "RECONSTRUCTED LOGICAL ROWS"
    )

    print_separator("-")

    for i, row in enumerate(
        reconstructed
    ):

        print()
        print(
            f"LOGICAL ROW {i}"
        )

        values = (
            reconstructed_row_text(
                row
            )
        )

        for column, text in enumerate(
            values
        ):

            print(
                f"  COL {column}: "
                f"{text!r}"
            )


# ============================================================================
# ACETAMIDE / ACETAMIPRID DEBUG
# ============================================================================

def debug_acetamide(
    tables: list[dict[str, Any]],
):

    terms = [
        "Acetamide",
        "Acetamiprid",
    ]

    print()
    print_separator()

    print(
        "ACETAMIDE / ACETAMIPRID CHECK"
    )

    print_separator()

    found = False

    for table in tables:

        rows = table.get(
            "rows"
        ) or []

        for row_index, row in enumerate(
            rows
        ):

            if row_contains_chemical(
                row,
                terms,
            ):

                found = True

                print()
                print(
                    "TABLE:",
                    table.get(
                        "table_id"
                    ),
                )

                print(
                    "PAGE:",
                    table.get(
                        "page_number"
                    ),
                )

                print(
                    "ROW:",
                    row_index,
                )

                print(
                    "COLS:",
                    len(row),
                )

                for cell in row:

                    print(
                        f"  COL "
                        f"{cell.get('column')}: "
                        f"{cell_text(cell)!r}"
                    )

                    print(
                        "      cell_id:",
                        cell.get(
                            "cell_id"
                        ),
                    )

                    print(
                        "      row:",
                        cell.get(
                            "row"
                        ),
                    )

                    print(
                        "      bbox:",
                        cell.get(
                            "bbox"
                        ),
                    )

                    print(
                        "      bbox_confidence:",
                        cell.get(
                            "bbox_confidence"
                        ),
                    )

                    print(
                        "      bbox_source:",
                        cell.get(
                            "bbox_source"
                        ),
                    )

                    print(
                        "      source_reference:",
                        json.dumps(
                            cell.get(
                                "source_reference"
                            ),
                            ensure_ascii=False,
                        ),
                    )

    if not found:

        print(
            "Acetamide / Acetamiprid "
            "not found."
        )


# ============================================================================
# GOLD DEBUG
# ============================================================================

def debug_gold():

    if not GOLD_PATH.exists():

        print()
        print(
            "GOLD FILE NOT FOUND:",
            GOLD_PATH,
        )

        return

    gold = load_json(
        GOLD_PATH
    )

    print()
    print_separator()

    print(
        "GOLD ACETAMIDE / ACETAMIPRID"
    )

    print_separator()

    pages = gold.get(
        "pages",
        {},
    )

    page = pages.get(
        "46"
    )

    if not page:

        print(
            "Gold page 46 not found."
        )

        return

    for table_index, table in enumerate(
        page.get("tables") or []
    ):

        for row in table.get(
            "rows"
        ) or []:

            chemical = str(
                row.get(
                    "chemical",
                    ""
                )
            )

            if (
                "Acetamide"
                in chemical
                or "Acetamiprid"
                in chemical
            ):

                print()
                print(
                    "TABLE:",
                    table_index,
                )

                print(
                    "row_number:",
                    row.get(
                        "row_number"
                    ),
                )

                print(
                    "chemical:",
                    row.get(
                        "chemical"
                    ),
                )

                print(
                    "CAS:",
                    row.get(
                        "cas"
                    ),
                )

                print(
                    "molecular_weight:",
                    row.get(
                        "molecular_weight"
                    ),
                )

                print(
                    "STEL/C:",
                    row.get(
                        "STEL/C"
                    ),
                )

                print(
                    "TWA:",
                    row.get(
                        "TWA"
                    ),
                )

                print(
                    "symbols:",
                    row.get(
                        "symbols"
                    ),
                )

                print(
                    "basis:",
                    row.get(
                        "basis"
                    ),
                )


# ============================================================================
# MAIN
# ============================================================================

def main():

    print_separator()

    print(
        "DEBUG LOGICAL ROW MAPPING"
    )

    print_separator()

    # ------------------------------------------------------------------
    # Load Layer 2
    # ------------------------------------------------------------------

    data = load_json(
        LAYER2_PATH
    )

    tables = data.get(
        "tables"
    ) or []

    print(
        "LAYER 2:",
        LAYER2_PATH,
    )

    print(
        "TOTAL TABLES:",
        len(tables),
    )

    # ------------------------------------------------------------------
    # Print basic table structure
    # ------------------------------------------------------------------

    print()
    print_separator()

    print(
        "TABLE STRUCTURE"
    )

    print_separator()

    for table in tables:

        print_table_summary(
            table
        )

    # ------------------------------------------------------------------
    # Find actual reconstruction cases
    # ------------------------------------------------------------------

    candidates = (
        find_reconstruction_candidates(
            tables
        )
    )

    # ------------------------------------------------------------------
    # Print every actual reconstruction case
    # ------------------------------------------------------------------

    for candidate in candidates:

        print_reconstruction_details(
            candidate
        )

    # ------------------------------------------------------------------
    # Acetamide / Acetamiprid
    # ------------------------------------------------------------------

    debug_acetamide(
        tables
    )

    # ------------------------------------------------------------------
    # Gold
    # ------------------------------------------------------------------

    debug_gold()

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------

    print()
    print_separator()

    print(
        "FINAL SUMMARY"
    )

    print_separator()

    print(
        "TOTAL TABLES:",
        len(tables),
    )

    print(
        "ACTUAL RECONSTRUCTION TABLES:",
        len(candidates),
    )

    for candidate in candidates:

        table = candidate[
            "table"
        ]

        print(
            f"  page={table.get('page_number')} "
            f"table={table.get('table_id')} "
            f"{candidate['source_count']} "
            f"-> "
            f"{candidate['output_count']}"
        )

    print()

    if not candidates:

        print(
            "NO TABLE HAD ROW COUNT REDUCTION."
        )

        print(
            "Therefore logical reconstruction "
            "did not merge any rows in Layer 2."
        )

    else:

        print(
            "At least one table was actually "
            "reconstructed."
        )

        print(
            "Inspect the reconstruction details above."
        )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()