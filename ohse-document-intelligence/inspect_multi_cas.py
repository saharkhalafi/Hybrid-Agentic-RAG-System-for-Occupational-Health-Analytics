import json
import re

GOLD_PATH = "tests/fixtures/oel_gold_46_55.json"
ACTUAL_PATH = "data/intermediate/validated_structure_46-55.json"

with open(GOLD_PATH, encoding="utf-8") as f:
    gold_data = json.load(f)

with open(ACTUAL_PATH, encoding="utf-8") as f:
    actual_data = json.load(f)


# ---------------------------------------------------------
# Build Gold index by (page, CAS)
# ---------------------------------------------------------

gold_rows = []

for page_str, page_data in gold_data["pages"].items():
    page = int(page_str)

    for table in page_data["tables"]:
        for row in table["rows"]:
            gold_rows.append({
                "page": page,
                "row_number": row["row_number"],
                "cas": row.get("cas", []),
                "chemical": row.get("chemical", ""),
                "molecular_weight": row.get("molecular_weight"),
                "STEL/C": row.get("STEL/C"),
                "TWA": row.get("TWA"),
                "symbols": row.get("symbols"),
                "basis": row.get("basis"),
            })


# ---------------------------------------------------------
# Actual rows
# ---------------------------------------------------------

print("=" * 100)
print("ACTUAL ROWS WITH MULTIPLE CAS vs GOLD")
print("=" * 100)

total = 0

for table in actual_data["tables"]:

    page = table["page_number"]

    # Skip header row
    for row in table["rows"][1:]:

        chemical = next(
            (
                c.get("normalized_value")
                for c in row
                if c.get("column") == 5
            ),
            ""
        )

        actual_row_number = next(
            (
                c.get("normalized_value")
                for c in row
                if c.get("column") == 6
            ),
            ""
        )

        actual_cas = re.findall(
            r"\[(\d{2,7}-\d{2}-\d)\]",
            chemical or ""
        )

        if len(actual_cas) <= 1:
            continue

        total += 1

        print()
        print("-" * 100)
        print("PAGE:", page)
        print("ACTUAL ROW:", actual_row_number)
        print("ACTUAL CAS:", actual_cas)
        print("ACTUAL CHEMICAL:", chemical)

        print()
        print("GOLD MATCHES:")

        matches = []

        for gold in gold_rows:

            if gold["page"] != page:
                continue

            if any(cas in gold["cas"] for cas in actual_cas):
                matches.append(gold)

        if not matches:
            print("  NO GOLD MATCH")
        else:
            for gold in matches:
                print(
                    "  GOLD row={}".format(gold["row_number"])
                )
                print(
                    "    CAS      :", gold["cas"]
                )
                print(
                    "    chemical :", gold["chemical"]
                )
                print(
                    "    MW       :", gold["molecular_weight"]
                )
                print(
                    "    STEL/C   :", gold["STEL/C"]
                )
                print(
                    "    TWA      :", gold["TWA"]
                )


print()
print("=" * 100)
print("TOTAL ACTUAL ROWS WITH MULTIPLE CAS:", total)
print("=" * 100)
