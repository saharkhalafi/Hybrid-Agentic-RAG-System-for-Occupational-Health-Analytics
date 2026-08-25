from __future__ import annotations

import json
from pathlib import Path


RAW_PATH = Path(
    r"E:\cursor projects\HSE6 AI Agent\ohse-document-intelligence"
    r"\data\intermediate\document_ai_raw_46-70.json"
)


def extract_text(cell: dict) -> str:
    parts = []

    for block in cell.get("blocks", []):
        text_block = block.get("textBlock", {})
        text = text_block.get("text")

        if text:
            parts.append(text)

    return " ".join(parts).strip()


def inspect_table(table: dict, table_index: int) -> None:
    print("\n" + "=" * 100)
    print(f"TABLE {table_index}")
    print("=" * 100)

    body_rows = table.get("bodyRows", [])

    print(f"Rows: {len(body_rows)}")

    for row_index, row in enumerate(body_rows):
        cells = row.get("cells", [])

        print(f"\nROW {row_index}")

        for col_index, cell in enumerate(cells):
            text = extract_text(cell)

            print(
                f"  COL {col_index}: "
                f"rowSpan={cell.get('rowSpan')} "
                f"colSpan={cell.get('colSpan')} "
                f"text={text!r}"
            )


def main() -> None:
    print(f"Reading: {RAW_PATH}")

    with RAW_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    blocks = data.get("documentLayout", {}).get("blocks", [])

    print(f"Top-level blocks: {len(blocks)}")

    table_count = 0

    for block in blocks:
        table_block = block.get("tableBlock")

        if not table_block:
            continue

        table_count += 1
        inspect_table(table_block, table_count)

    print("\n" + "=" * 100)
    print(f"TOTAL TABLES: {table_count}")
    print("=" * 100)


if __name__ == "__main__":
    main()