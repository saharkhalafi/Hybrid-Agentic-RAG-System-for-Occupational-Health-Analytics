import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

from goldset_generator.oel_row_parser import parse_layer2_molecular_weight
from pipeline_contracts.numeric_integrity import parse_numeric_cell


GOLD_XLSX = Path("goldset.xlsx")
ACTUAL_JSON = Path("data/intermediate/validated_structure_46-55.json")

CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
EASTERN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
MISSING_VALUES = {"", "—", "–", "-"}
LIMIT_UNIT_RE = re.compile(
    r"(ppm|f/ml|mg/m(?:³|3|\^\{?3\}?|\^3))",
    re.IGNORECASE,
)
NUMERIC_TOKEN_RE = re.compile(r"\d+(?:[./]\d+)?")


# ============================================================================
# NORMALIZATION
# ============================================================================

def normalize_text(value):
    if value is None:
        return ""

    text = str(value).strip()

    # Excel em dash = missing value
    if text in {"—", "–", "-", ""}:
        return ""

    return " ".join(text.split()).lower()


def normalize_cas(value):
    if value is None:
        return []

    text = str(value)

    matches = CAS_RE.findall(text)

    if matches:
        return sorted(set(matches))

    return sorted(
        set(
            part.strip()
            for part in text.replace("[", "").replace("]", "").split(";")
            if part.strip()
        )
    )


def _eval_text(value):
    if value is None:
        return ""

    if isinstance(value, bool):
        return str(value)

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return format(value, ".12g")

    return str(value).strip()


def _latin_digits(value):
    return _eval_text(value).translate(EASTERN_DIGITS)


def _is_missing(value):
    return _eval_text(value) in MISSING_VALUES


def _parse_eval_decimal(value):
    """Parse a numeric evaluation token after Eastern-digit and slash-decimal normalization."""

    if _is_missing(value):
        return None

    text = _latin_digits(value)
    text = text.replace(",", "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("/", ".")

    match = NUMERIC_TOKEN_RE.search(text)
    if not match:
        return None

    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def normalize_number(value):
    """
    Normalize MW-like values such as:
        183.16
        ۱۸۳/۱۶
        183/16
        183 / 16
    """

    if _is_missing(value):
        return ""

    text = _latin_digits(value)
    text = text.replace(",", "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("/", ".")

    parsed = _parse_eval_decimal(text)
    if parsed is None:
        return text

    return format(parsed, "f")


def normalize_mw(value):
    """Evaluate MW using the molecular-weight parser (RTL slash order)."""

    if _is_missing(value):
        return ""

    parsed = parse_layer2_molecular_weight(_latin_digits(value))
    if parsed is not None:
        return format(Decimal(parsed), "f")

    return normalize_number(value)


def _canonical_limit_unit(raw_unit):
    if not raw_unit:
        return None

    unit = raw_unit.lower().replace("³", "3")
    unit = unit.replace("^{3}", "3").replace("^3", "3")
    unit = unit.replace("{", "").replace("}", "")

    if unit.startswith("mg/m"):
        return "mg/m3"

    return unit


def _parse_eval_limit(value):
    """Return (number, unit) for STEL/TWA evaluation. unit is None when absent."""

    if _is_missing(value):
        return None, None

    result = parse_numeric_cell(_latin_digits(value), field_type="TWA")
    unit = _canonical_limit_unit(result.unit)

    if result.normalized_value:
        try:
            return Decimal(result.normalized_value), unit
        except InvalidOperation:
            pass

    text = _latin_digits(value)
    text = text.replace("³", "3")
    text = re.sub(r"\s+", "", text)

    unit_match = LIMIT_UNIT_RE.search(text)
    if unit is None:
        unit = _canonical_limit_unit(unit_match.group(1)) if unit_match else None
    number_text = text[: unit_match.start()] + text[unit_match.end() :] if unit_match else text

    number = _parse_eval_decimal(number_text)
    return number, unit


def normalize_limit(value):
    """
    Normalize STEL/TWA for evaluation display.

    Comparison uses compare_field(), which matches on the numeric value and
    only compares units when both sides have an explicit unit.
    """

    if _is_missing(value):
        return ""

    text = _latin_digits(value)
    text = text.replace("³", "3")
    text = re.sub(r"\s+", "", text)

    # Normalize common PDF/Excel variants.
    text = text.replace("mg/m3", "mg/m³")
    text = text.replace("mg/m^3", "mg/m³")
    text = text.replace("mg/m^{3}", "mg/m³")

    return text.lower()


# ============================================================================
# EXCEL GOLD
# ============================================================================

def load_excel_gold():
    assert GOLD_XLSX.exists(), f"Gold Excel not found: {GOLD_XLSX}"

    workbook = openpyxl.load_workbook(
        GOLD_XLSX,
        data_only=True,
    )

    sheet = workbook.active

    headers = [
        str(cell.value).strip()
        if cell.value is not None
        else ""
        for cell in sheet[1]
    ]

    required = [
        "page",
        "row",
        "CAS",
        "chemical_name",
        "STEL",
        "TWA",
        "MW",
    ]

    missing = [
        column
        for column in required
        if column not in headers
    ]

    assert not missing, (
        f"Gold Excel missing columns: {missing}\n"
        f"Found columns: {headers}"
    )

    index = {
        name: headers.index(name)
        for name in required
    }

    rows = []

    for excel_row_number, row in enumerate(
        sheet.iter_rows(min_row=2, values_only=True),
        start=2,
    ):
        if not row:
            continue

        page = row[index["page"]]

        if page is None:
            continue

        rows.append(
            {
                "excel_row": excel_row_number,
                "page": int(page),
                "row": row[index["row"]],
                "cas": normalize_cas(row[index["CAS"]]),
                "chemical_name": normalize_text(
                    row[index["chemical_name"]]
                ),
                "STEL": normalize_limit(
                    row[index["STEL"]]
                ),
                "TWA": normalize_limit(
                    row[index["TWA"]]
                ),
                "MW": normalize_number(
                    row[index["MW"]]
                ),
            }
        )

    return rows


# ============================================================================
# ACTUAL LAYER 2
# ============================================================================

def load_actual():
    assert ACTUAL_JSON.exists(), (
        f"Actual Layer 2 JSON not found: {ACTUAL_JSON}"
    )

    with open(
        ACTUAL_JSON,
        encoding="utf-8",
    ) as f:
        actual = json.load(f)

    return actual


def extract_actual_rows(actual):
    result = []

    for table_index, table in enumerate(
        actual.get("tables", [])
    ):
        page = int(table["page_number"])

        rows = table.get("rows", [])

        # Skip header row.
        for row_index, row in enumerate(
            rows[1:],
            start=1,
        ):
            if not row:
                continue

            values = {}

            for cell in row:
                if not isinstance(cell, dict):
                    continue

                column = cell.get("column")

                if column is None:
                    continue

                values[int(column)] = cell.get(
                    "normalized_value"
                )

            # ------------------------------------------------------------
            # Extract CAS from the entire physical row.
            # ------------------------------------------------------------

            row_text_parts = []

            for cell in row:
                if not isinstance(cell, dict):
                    continue

                for key in (
                    "text",
                    "normalized_value",
                    "normalized_text",
                ):
                    value = cell.get(key)

                    if value:
                        row_text_parts.append(str(value))

            row_text = " ".join(row_text_parts)

            cas = normalize_cas(row_text)

            chemical = normalize_text(
                values.get(5)
            )

            # Ignore completely empty rows.
            if not cas and not chemical:
                continue

            result.append(
                {
                    "page": page,
                    "table_index": table_index,
                    "row_index": row_index,
                    "cas": cas,
                    "chemical_name": chemical,
                    "STEL": normalize_limit(
                        values.get(2)
                    ),
                    "TWA": normalize_limit(
                        values.get(3)
                    ),
                    "MW": normalize_mw(
                        values.get(4)
                    ),
                }
            )

    return result


# ============================================================================
# MATCHING
# ============================================================================

def find_actual_row(gold_row, actual_rows):
    """
    Primary identity:
        page + CAS

    CAS is used instead of physical row number because Layer 2 may
    reconstruct/split rows and therefore physical row indexes can change.
    """

    gold_cas = set(gold_row["cas"])

    if not gold_cas:
        return None

    candidates = [
        row
        for row in actual_rows
        if row["page"] == gold_row["page"]
        and gold_cas.intersection(row["cas"])
    ]

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        # Prefer an exact CAS-set match.
        exact = [
            row
            for row in candidates
            if set(row["cas"]) == gold_cas
        ]

        if len(exact) == 1:
            return exact[0]

    return None


# ============================================================================
# FIELD COMPARISON
# ============================================================================

FIELDS = [
    "chemical_name",
    "STEL",
    "TWA",
    "MW",
]


def compare_field(field, expected, actual):
    if field == "CAS":
        return normalize_cas(expected) == normalize_cas(actual)

    if field == "MW":
        expected_missing = _is_missing(expected)
        actual_missing = _is_missing(actual)
        if expected_missing and actual_missing:
            return True
        if expected_missing or actual_missing:
            return False

        expected_number = _parse_eval_decimal(expected)
        actual_number = _parse_eval_decimal(actual)
        if expected_number is not None and actual_number is not None:
            return expected_number == actual_number

        return normalize_number(expected) == normalize_number(actual)

    if field in {"STEL", "TWA"}:
        expected_missing = _is_missing(expected)
        actual_missing = _is_missing(actual)
        if expected_missing and actual_missing:
            return True
        if expected_missing or actual_missing:
            return False

        expected_number, expected_unit = _parse_eval_limit(expected)
        actual_number, actual_unit = _parse_eval_limit(actual)
        if expected_number is None or actual_number is None:
            return False
        if expected_number != actual_number:
            return False
        if expected_unit and actual_unit:
            return expected_unit == actual_unit
        return True

    return normalize_text(expected) == normalize_text(actual)


# ============================================================================
# TEST 1 — GOLD LOAD
# ============================================================================

def test_goldset_excel_is_valid():
    rows = load_excel_gold()

    assert rows, "Gold Excel contains no data rows."

    pages = {row["page"] for row in rows}

    assert pages == set(range(46, 56)), (
        f"Gold Excel pages are incorrect: {sorted(pages)}"
    )


# ============================================================================
# TEST 2 — EVERY GOLD ROW EXISTS
# ============================================================================

def test_every_gold_row_exists_in_actual():
    gold = load_excel_gold()
    actual = extract_actual_rows(load_actual())

    missing = []

    for gold_row in gold:
        # فقط صفحه 46
        if gold_row["page"] != 46:
            continue

        actual_row = find_actual_row(
            gold_row,
            actual,
        )

        if actual_row is None:
            missing.append(
                {
                    "excel_row": gold_row["excel_row"],
                    "page": gold_row["page"],
                    "row": gold_row["row"],
                    "CAS": gold_row["cas"],
                    "chemical": gold_row["chemical_name"],
                }
            )

    print("\n" + "=" * 80)
    print("GOLD ROW EXISTENCE CHECK — PAGE 46")
    print("=" * 80)

    print(f"Gold rows on page 46: {sum(1 for r in gold if r['page'] == 46)}")
    print(f"Missing rows on page 46: {len(missing)}")

    if missing:
        print("\nMissing rows:")
        for item in missing:
            print(
                f"  Excel row : {item['excel_row']}\n"
                f"  Page      : {item['page']}\n"
                f"  Gold row  : {item['row']}\n"
                f"  CAS       : {item['CAS']}\n"
                f"  Chemical  : {item['chemical']}\n"
                f"  {'-' * 60}"
            )
    else:
        print("\nALL GOLD ROWS FROM PAGE 46 EXIST IN ACTUAL.")

    print("=" * 80)

    assert not missing, (
        "\nGold rows missing from Actual Layer 2 (Page 46):\n"
        + "\n".join(
            str(item)
            for item in missing
        )
    )



# ============================================================================
# TEST 3 — EXACT FIELD VALUES
# ============================================================================

def test_gold_values_match_actual():
    gold = load_excel_gold()
    actual = extract_actual_rows(load_actual())

    failures = []

    for gold_row in gold:
        actual_row = find_actual_row(
            gold_row,
            actual,
        )

        if actual_row is None:
            continue

        for field in FIELDS:
            expected = gold_row[field]
            actual_value = actual_row[field]

            if not compare_field(
                field,
                expected,
                actual_value,
            ):
                failures.append(
                    {
                        "excel_row": gold_row["excel_row"],
                        "page": gold_row["page"],
                        "row": gold_row["row"],
                        "CAS": gold_row["cas"],
                        "field": field,
                        "expected": expected,
                        "actual": actual_value,
                        "actual_row_index": actual_row[
                            "row_index"
                        ],
                    }
                )

    assert not failures, (
        "\nGold vs Actual mismatches:\n"
        + "\n".join(
            (
                f"PAGE {item['page']} | "
                f"EXCEL ROW {item['excel_row']} | "
                f"CAS {item['CAS']} | "
                f"FIELD {item['field']} | "
                f"EXPECTED={item['expected']!r} | "
                f"ACTUAL={item['actual']!r} | "
                f"ACTUAL_ROW={item['actual_row_index']}"
            )
            for item in failures
        )
    )


# ============================================================================
# TEST 4 — GOLD CAS MUST BE UNIQUE
# ============================================================================

def test_gold_cas_are_unique():
    gold = load_excel_gold()

    seen = {}

    for row in gold:
        identity = (
            row["page"],
            row["row"],
        )

        for cas in row["cas"]:
            seen.setdefault(
                cas,
                [],
            ).append(identity)

    duplicates = {
        cas: locations
        for cas, locations in seen.items()
        if len(set(locations)) > 1
    }

    assert not duplicates, (
        "\nSame CAS appears in multiple Gold rows:\n"
        + str(duplicates)
    )


# ============================================================================
# TEST 5 — NO GOLD ROW MERGING
# ============================================================================

def test_actual_does_not_merge_independent_gold_rows():
    gold = load_excel_gold()
    actual = extract_actual_rows(load_actual())

    gold_by_cas = {}

    for gold_row in gold:
        identity = (
            gold_row["page"],
            gold_row["row"],
        )

        for cas in gold_row["cas"]:
            gold_by_cas.setdefault(
                cas,
                set(),
            ).add(identity)

    failures = []

    for actual_row in actual:
        represented_gold_rows = set()

        for cas in actual_row["cas"]:
            represented_gold_rows.update(
                gold_by_cas.get(cas, set())
            )

        if len(represented_gold_rows) > 1:
            failures.append(
                {
                    "page": actual_row["page"],
                    "actual_row": actual_row["row_index"],
                    "actual_cas": actual_row["cas"],
                    "gold_rows": sorted(
                        represented_gold_rows
                    ),
                }
            )

    assert not failures, (
        "\nActual Layer 2 merged independent Gold rows:\n"
        + "\n".join(
            str(item)
            for item in failures
        )
    )