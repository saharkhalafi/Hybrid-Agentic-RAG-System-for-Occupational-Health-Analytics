from __future__ import annotations

import json
import re
from pathlib import Path

import fitz


PDF_PATH = Path("../OHE6.pdf")
JSON_PATH = Path(
    "data/intermediate/validated_structure_46-55.json"
)

START_PAGE = 46
END_PAGE = 55


CAS_RE = re.compile(
    r"\[\d{2,7}-\d{2}-\d\]"
)


def normalize(text: str | None) -> str:
    if not text:
        return ""

    text = str(text)

    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "\u200c": " ",
        "\xa0": " ",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split()).strip()


def load_validated_structure():
    with JSON_PATH.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def get_page_tables(data, page_number):
    return [
        table
        for table in data.get("tables", [])
        if table.get("page_number") == page_number
    ]


def print_pdf_page(page):
    print()
    print("=" * 120)
    print(f"PDF PAGE {page.number + 1}")
    print("=" * 120)

    text = page.get_text("text")

    print(text)


def print_extracted_table(table):
    print()
    print("-" * 120)
    print(
        f"EXTRACTED TABLE: {table.get('table_id')}"
    )
    print("-" * 120)

    rows = table.get("rows", [])

    for row_index, row in enumerate(rows):

        print(f"\nROW {row_index}")

        for cell in row:

            print(
                f"  [{cell.get('column')}] "
                f"{cell.get('text', '')!r}"
            )


def validate_structure_shape(table):
    rows = table.get("rows", [])

    errors = []

    if not rows:
        errors.append(
            "table has no rows"
        )
        return errors

    for row_index, row in enumerate(rows):

        columns = sorted(
            cell.get("column")
            for cell in row
            if cell.get("column") is not None
        )

        expected = list(range(7))

        if columns != expected:
            errors.append(
                f"row {row_index}: "
                f"columns={columns}, "
                f"expected={expected}"
            )

    return errors


def get_cell(table, row_index, column_index):
    rows = table.get("rows", [])

    if row_index >= len(rows):
        return None

    for cell in rows[row_index]:

        if cell.get("column") == column_index:
            return cell

    return None


def cell_text(table, row, col):
    cell = get_cell(table, row, col)

    if cell is None:
        return ""

    return normalize(
        cell.get("text")
    )


def print_oel_semantic_view(table):
    """
    Print the table in the actual OEL semantic schema.

    0 = basis
    1 = symbols
    2 = STEL/C
    3 = TWA
    4 = molecular weight
    5 = chemical
    6 = row number
    """

    print()
    print("=" * 120)
    print("SEMANTIC OEL VIEW")
    print("=" * 120)

    rows = table.get("rows", [])

    for row_index in range(len(rows)):

        values = {
            "basis": cell_text(
                table,
                row_index,
                0,
            ),
            "symbols": cell_text(
                table,
                row_index,
                1,
            ),
            "STEL/C": cell_text(
                table,
                row_index,
                2,
            ),
            "TWA": cell_text(
                table,
                row_index,
                3,
            ),
            "molecular_weight": cell_text(
                table,
                row_index,
                4,
            ),
            "chemical": cell_text(
                table,
                row_index,
                5,
            ),
            "row_number": cell_text(
                table,
                row_index,
                6,
            ),
        }

        print(
            f"\nLOGICAL ROW {row_index}"
        )

        for key, value in values.items():
            print(
                f"  {key:20s}: {value!r}"
            )


def main():

    print(
        f"Loading PDF: {PDF_PATH}"
    )

    doc = fitz.open(PDF_PATH)

    data = load_validated_structure()

    print()
    print("=" * 120)
    print(
        f"VALIDATING PAGES "
        f"{START_PAGE}-{END_PAGE}"
    )
    print("=" * 120)

    total_errors = 0

    for page_number in range(
        START_PAGE,
        END_PAGE + 1,
    ):

        page = doc[
            page_number - 1
        ]

        tables = get_page_tables(
            data,
            page_number,
        )

        print()
        print("#" * 120)
        print(
            f"PAGE {page_number}"
        )
        print("#" * 120)

        print(
            f"Document AI / Layer 2 tables: "
            f"{len(tables)}"
        )

        if not tables:
            print(
                "WARNING: No extracted table"
            )
            continue

        for table in tables:

            print_extracted_table(
                table
            )

            errors = validate_structure_shape(
                table
            )

            if errors:

                print()
                print(
                    "STRUCTURAL ERRORS:"
                )

                for error in errors:
                    print(
                        f"  ERROR: {error}"
                    )

                total_errors += len(
                    errors
                )

            else:

                print()
                print(
                    "STRUCTURE: PASS"
                )

            print_oel_semantic_view(
                table
            )

        print()
        print(
            "RAW PDF TEXT:"
        )

        print_pdf_page(
            page
        )

    doc.close()

    print()
    print("=" * 120)
    print("FINAL RESULT")
    print("=" * 120)

    if total_errors == 0:
        print(
            "STRUCTURAL VALIDATION: PASS"
        )
    else:
        print(
            f"STRUCTURAL VALIDATION: "
            f"FAIL ({total_errors} errors)"
        )


if __name__ == "__main__":
    main()