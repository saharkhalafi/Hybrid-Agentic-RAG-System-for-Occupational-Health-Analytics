import json
import re

GOLD_PATH = "tests/fixtures/oel_gold_46-55.json"
ACTUAL_PATH = "data/intermediate/validated_structure_46-55.json"

with open(GOLD_PATH, encoding="utf-8") as f:
    gold_data = json.load(f)

with open(ACTUAL_PATH, encoding="utf-8") as f:
    actual_data = json.load(f)


# ------------------------------------------------------------
# Build GOLD index
# ------------------------------------------------------------

gold_rows = []

for page_number, page_data in gold_data["pages"].items():
    page_number = int(page_number)

    for table in page_data["tables"]:
        for row in table["rows"]:
            gold_rows.append({
                "page": page_number,
                "row": row["row_number"],
                "cas": row.get("cas", []),
                "chemical": row.get("chemical", ""),
            })


# ------------------------------------------------------------
# Helper
# ------------------------------------------------------------

def get_column(row, column):
    for cell in row:
        if cell.get("column") == column:
            return cell.get("normalized_value")
    return ""


def extract_cas(text):
    if not text:
        return []

    return re.findall(
        r"\[(\d{2,7}-\d{2}-\d)\]",
        str(text)
    )


# ------------------------------------------------------------
# Compare
# ------------------------------------------------------------

print("=" * 100)
print("ACTUAL ROWS WITH MULTIPLE CAS vs GOLD")
print("=" * 100)


suspicious_count = 0


for table in actual_data["tables"]:

    page = table["page_number"]

    for row in table["rows"][1:]:

        if not row:
            continue

        chemical = get_column(row, 5)
        row_number = get_column(row, 6)

        cas_list = extract_cas(chemical)

        if len(cas_list) <= 1:
            continue

        suspicious_count += 1

        print()
        print("-" * 100)
        print("PAGE:", page)
        print("ACTUAL ROW:", row_number)
        print("ACTUAL CHEMICAL:", chemical)
        print("ACTUAL CAS:", cas_list)

        print()
        print("GOLD MATCHES:")

        matches = []

        for g in gold_rows:

            if g["page"] != page:
                continue

            if any(cas in g["cas"] for cas in cas_list):
                matches.append(g)

        if not matches:
            print("  !!! NO GOLD MATCH FOUND !!!")

        else:
            for g in matches:
                print(
                    "  GOLD row=",
                    g["row"],
                    "CAS=",
                    g["cas"],
                    "chemical=",
                    g["chemical"]
                )


print()
print("=" * 100)
print("TOTAL ACTUAL MULTI-CAS ROWS:", suspicious_count)
print("=" * 100)
