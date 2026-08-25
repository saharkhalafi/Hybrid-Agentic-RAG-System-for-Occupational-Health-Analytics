from __future__ import annotations

import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def print_table(table: dict) -> None:
    print("\n" + "=" * 100)
    print(f"TABLE: {table.get('table_id')}")
    print(f"PAGE : {table.get('page_number')}")
    print(f"TYPE : {table.get('table_type')}")
    print(f"CONF : {table.get('structural_confidence')}")
    print("=" * 100)

    rows = table.get("rows") or []

    for row_index, row in enumerate(rows):
        print(f"\nROW {row_index}")

        cells = sorted(
            row,
            key=lambda c: int(c.get("column", 0)),
        )

        for cell in cells:
            col = cell.get("column")
            text = cell.get("text") or ""
            source = cell.get("source")
            bbox_source = cell.get("bbox_source")
            bbox = cell.get("bbox")

            print(
                f"  [{col}] "
                f"text={text!r} "
                f"| source={source!r} "
                f"| bbox_source={bbox_source!r} "
                f"| bbox={bbox}"
            )


def print_oel_schema(data: dict, page: int) -> None:
    detection = (
        data.get("page_detection", {})
        .get(str(page))
    )

    if detection is None:
        detection = (
            data.get("page_detection", {})
            .get(page)
        )

    print("\n" + "-" * 100)
    print(f"PAGE {page} OEL SCHEMA")
    print("-" * 100)

    if not detection:
        print("NO PAGE DETECTION")
        return

    schema = detection.get("oel_schema")

    if not schema:
        print("NO OEL SCHEMA")
        return

    print(
        json.dumps(
            schema,
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: "
            "python scripts/inspect_validated_tables.py "
            "data/intermediate/validated_structure_46-55.json "
            "[page]"
        )
        raise SystemExit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        raise FileNotFoundError(path)

    data = load_json(path)

    tables = data.get("tables") or []

    page_filter = None

    if len(sys.argv) >= 3:
        page_filter = int(sys.argv[2])

    selected = []

    for table in tables:
        page = int(table.get("page_number"))

        if page_filter is None or page == page_filter:
            selected.append(table)

    print(
        f"\nFound {len(selected)} tables"
        + (
            f" on page {page_filter}"
            if page_filter is not None
            else ""
        )
    )

    for table in selected:
        print_table(table)

    if page_filter is not None:
        print_oel_schema(
            data,
            page_filter,
        )


if __name__ == "__main__":
    main()