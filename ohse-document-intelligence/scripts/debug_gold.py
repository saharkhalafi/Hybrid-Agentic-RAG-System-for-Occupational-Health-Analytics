import json
from pathlib import Path


GOLD_PATH = Path("tests/fixtures/oel_gold_46_55.json")


def main():
    data = json.loads(GOLD_PATH.read_text(encoding="utf-8"))

    for page_key, page in data["pages"].items():
        print()
        print("=" * 100)
        print(f"PAGE {page_key}")
        print("=" * 100)

        for table in page["tables"]:
            for row in table["rows"]:
                print()
                print(f"ROW {row.get('row_number')}")

                print(f"  chemical : {row.get('chemical')}")
                print(f"  CAS      : {row.get('cas')}")
                print(f"  MW       : {row.get('molecular_weight')}")
                print(f"  STEL/C   : {row.get('STEL/C')}")
                print(f"  TWA      : {row.get('TWA')}")
                print(f"  symbols  : {row.get('symbols')}")
                print(f"  basis    : {row.get('basis')}")


if __name__ == "__main__":
    main()