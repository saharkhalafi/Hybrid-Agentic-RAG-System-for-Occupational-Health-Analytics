import json
import sys
from pathlib import Path


def inspect(obj, indent=0, max_items=10):
    prefix = " " * indent

    if isinstance(obj, dict):
        items = list(obj.items())

        for i, (key, value) in enumerate(items[:max_items]):
            if isinstance(value, (dict, list)):
                print(f"{prefix}{key}: {type(value).__name__}")

                if isinstance(value, list):
                    print(f"{prefix}  length = {len(value)}")
                else:
                    print(f"{prefix}  keys = {list(value.keys())[:20]}")

                inspect(value, indent + 4, max_items)

            else:
                print(f"{prefix}{key}: {value!r}")

        if len(items) > max_items:
            print(
                f"{prefix}... {len(items) - max_items} more keys"
            )

    elif isinstance(obj, list):
        print(f"{prefix}LIST length = {len(obj)}")

        for i, item in enumerate(obj[:max_items]):
            print(f"{prefix}[{i}] {type(item).__name__}")

            if isinstance(item, (dict, list)):
                inspect(item, indent + 4, max_items)
            else:
                print(f"{prefix}    {item!r}")

        if len(obj) > max_items:
            print(
                f"{prefix}... {len(obj) - max_items} more items"
            )


def main():
    if len(sys.argv) != 2:
        print(
            'Usage:\n'
            'python scripts/inspect_semantic_table.py '
            '"data/canonical/tables/.../table_046_01.json"'
        )
        sys.exit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"ERROR: File does not exist:\n{path}")
        sys.exit(1)

    print("=" * 100)
    print(f"FILE: {path}")
    print("=" * 100)

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    print("\nTYPE:")
    print(type(data).__name__)

    if isinstance(data, dict):
        print("\nTOP-LEVEL KEYS:")
        for key in data:
            print(f"  - {key}")

    print("\nSTRUCTURE:")
    inspect(data)


if __name__ == "__main__":
    main()