import json
import re

path = "data/intermediate/validated_structure_46-55.json"

with open(path, encoding="utf-8") as f:
    d = json.load(f)

cas_pattern = re.compile(r"\b\d{2,7}-\d{2}-\d\b")

print("=" * 100)
print("CAS MAPPING AUDIT — PAGES 46-55")
print("=" * 100)

for table in d["tables"]:
    page = int(table["page_number"])

    rows = sorted(set(c["row"] for c in table["cells"]))

    for row in rows:
        values = []

        for cell in table["cells"]:
            if cell["row"] != row:
                continue

            text = str(cell.get("text") or "")
            norm = str(cell.get("normalized_value") or "")
            values.append(text + " " + norm)

        combined = " ".join(values)
        found_cas = sorted(set(cas_pattern.findall(combined)))

        if found_cas:
            print(
                f"PAGE {page:02d} | "
                f"ROW {row:02d} | "
                f"CAS={found_cas}"
            )

print("=" * 100)
print("DONE")
