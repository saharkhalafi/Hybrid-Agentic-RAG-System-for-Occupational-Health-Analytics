import json
import re
from pathlib import Path


GOLD_PATH = Path("tests/fixtures/oel_gold_46_55.json")
ACTUAL_PATH = Path("data/intermediate/validated_structure_46-55.json")
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")

def _ascii_digits(value):
    return str(value or "").translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_cas(text):
    if not text:
        return []

    return CAS_RE.findall(str(text))


def normalize_cas_list(values):
    if not values:
        return []

    normalized = []

    for value in values:
        if not value:
            continue

        matches = extract_cas(str(value))

        if matches:
            normalized.extend(matches)
        else:
            normalized.append(
                str(value).strip().strip("[]")
            )

    return sorted(set(normalized))


# ============================================================================
# GOLD
# ============================================================================

def gold_rows(gold):
    result = []

    for page_key, page in gold["pages"].items():
        page_number = int(page_key)

        for table_index, table in enumerate(page["tables"]):

            for row_index, row in enumerate(table["rows"]):

                cas = normalize_cas_list(
                    row.get("cas")
                )

                if not cas:
                    continue

                result.append({
                    "page": page_number,
                    "table_index": table_index,
                    "row_index": row_index,
                    "row_number": row.get("row_number"),
                    "cas": cas,
                    "chemical": row.get("chemical"),
                    "molecular_weight": row.get(
                        "molecular_weight"
                    ),
                    "STEL/C": row.get("STEL/C"),
                    "TWA": row.get("TWA"),
                    "symbols": row.get("symbols"),
                    "basis": row.get("basis"),
                })

    return result


# ============================================================================
# ACTUAL LAYER 2
# ============================================================================

def actual_rows(actual):
    result = []

    for table_index, table in enumerate(
        actual.get("tables", [])
    ):
        page = table["page_number"]

        rows = table.get("rows", [])

        # First row is the header.
        for row_index, row in enumerate(
            rows[1:],
            start=1,
        ):
            if not row:
                continue

            values = {
                c.get("column"): c.get("normalized_value")
                for c in row
                if isinstance(c, dict)
            }

            # ------------------------------------------------------------
            # Chemical column
            # ------------------------------------------------------------

            chemical = values.get(5)

            # ------------------------------------------------------------
            # CAS must be detected from the entire row.
            #
            # This prevents the test from silently missing a CAS that
            # Document AI placed in another column.
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
                        row_text_parts.append(
                            str(value)
                        )

            row_text = " ".join(row_text_parts)

            cas = normalize_cas_list(
                extract_cas(row_text)
            )

            # ------------------------------------------------------------
            # Ignore genuinely empty rows.
            #
            # Empty-row validation is handled separately below.
            # ------------------------------------------------------------

            if not chemical and not cas:
                continue

            result.append({
                "page": page,
                "table_index": table_index,
                "row_index": row_index,
                "row_number": values.get(6),
                "cas": cas,
                "chemical": chemical,
                "MW": values.get(4),
                "STEL/C": values.get(2),
                "TWA": values.get(3),
                "symbols": values.get(1),
                "basis": values.get(0),
            })

    return result


# ============================================================================
# EMPTY ROW DETECTION
# ============================================================================

def actual_empty_rows(actual):
    empty = []

    for table_index, table in enumerate(
        actual.get("tables", [])
    ):
        page = table.get("page_number")

        rows = table.get("rows", [])

        # Skip header.
        for row_index, row in enumerate(
            rows[1:],
            start=1,
        ):
            if not row:
                empty.append({
                    "page": page,
                    "table_index": table_index,
                    "row_index": row_index,
                })
                continue

            has_content = False

            for cell in row:
                if not isinstance(cell, dict):
                    continue

                for key in (
                    "text",
                    "normalized_value",
                    "normalized_text",
                ):
                    value = cell.get(key)

                    if value not in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        has_content = True
                        break

                if has_content:
                    break

            if not has_content:
                empty.append({
                    "page": page,
                    "table_index": table_index,
                    "row_index": row_index,
                })

    return empty


# ============================================================================
# TESTS
# ============================================================================

def test_gold_row_count():
    gold = load_json(GOLD_PATH)

    rows = gold_rows(gold)

    expected = 78

    assert len(rows) == expected, (
        f"Gold row count changed: "
        f"expected {expected}, got {len(rows)}"
    )


def test_actual_has_no_unexpected_empty_rows():
    actual = load_json(ACTUAL_PATH)

    empty_rows = actual_empty_rows(actual)

    assert not empty_rows, (
        "Unexpected empty rows in actual Layer 2:\n"
        + "\n".join(
            str(row)
            for row in empty_rows
        )
    )


# ============================================================================
# CRITICAL STRUCTURAL TEST
# ============================================================================

def test_actual_rows_are_not_merging_independent_gold_rows():
    """
    Ensure that multiple CAS values from independent Gold rows are not
    collapsed into one Actual Layer-2 row.

    Example of a real failure:

        GOLD:
            row 24 -> CAS 309-00-0
            row 25 -> CAS 107-18-6

        ACTUAL:
            row 24 25 -> CAS [309-00-0, 107-18-6]

    This is a genuine structural merge and must fail.
    """

    gold = load_json(GOLD_PATH)
    actual = load_json(ACTUAL_PATH)

    g_rows = gold_rows(gold)
    a_rows = actual_rows(actual)

    # ------------------------------------------------------------------
    # CAS -> Gold row identities
    #
    # IMPORTANT:
    # Do NOT use only (page, row_number).
    #
    # row_number may legitimately be None or may have been altered by
    # an upstream semantic numbering step.
    #
    # Physical identity is:
    #     page + table + source row index
    # ------------------------------------------------------------------

    gold_by_cas = {}

    for g in g_rows:
        identity = (
            g["page"],
            g["table_index"],
            g["row_index"],
        )

        for cas in g["cas"]:
            gold_by_cas.setdefault(
                cas,
                []
            ).append(
                (
                    identity,
                    g,
                )
            )

    failures = []

    for actual_row in a_rows:

        # A single CAS cannot represent a merge.
        if len(actual_row["cas"]) <= 1:
            continue

        matched_gold = []

        for cas in actual_row["cas"]:
            matched_gold.extend(
                gold_by_cas.get(
                    cas,
                    [],
                )
            )

        # --------------------------------------------------------------
        # Unique Gold physical rows represented by this Actual row.
        # --------------------------------------------------------------

        unique_gold = {}

        for identity, gold_row in matched_gold:
            unique_gold[identity] = gold_row

        # --------------------------------------------------------------
        # More than one independent Gold row collapsed into one Actual
        # row.
        # --------------------------------------------------------------

        if len(unique_gold) > 1:

            failures.append({
                "page": actual_row["page"],
                "table_index": actual_row["table_index"],
                "actual_row_index": actual_row[
                    "row_index"
                ],
                "actual_row_number": actual_row[
                    "row_number"
                ],
                "actual_cas": actual_row["cas"],
                "gold_rows": [
                    {
                        "page": gold_row["page"],
                        "table_index": gold_row[
                            "table_index"
                        ],
                        "source_row_index": gold_row[
                            "row_index"
                        ],
                        "row_number": gold_row[
                            "row_number"
                        ],
                        "cas": gold_row["cas"],
                        "chemical": gold_row[
                            "chemical"
                        ],
                    }
                    for gold_row in unique_gold.values()
                ],
            })

    if failures:

        message = "\n\n".join(
            [
                f"PAGE {failure['page']}\n"
                f"TABLE: {failure['table_index']}\n"
                f"ACTUAL ROW INDEX: "
                f"{failure['actual_row_index']}\n"
                f"ACTUAL ROW NUMBER: "
                f"{failure['actual_row_number']}\n"
                f"ACTUAL CAS: "
                f"{failure['actual_cas']}\n"
                f"GOLD ROWS:\n"
                + "\n".join(
                    (
                        f"  source_row={g['source_row_index']} "
                        f"row_number={g['row_number']} "
                        f"CAS={g['cas']} "
                        f"chemical={g['chemical']}"
                    )
                    for g in failure["gold_rows"]
                )
                for failure in failures
            ]
        )

        raise AssertionError(
            "\n\n"
            "Actual Layer 2 contains merged independent "
            "Gold rows:\n\n"
            + message
        )


# ============================================================================
# GOLD -> ACTUAL CAS COVERAGE
# ============================================================================

def test_every_gold_cas_exists_in_actual():
    gold = load_json(GOLD_PATH)
    actual = load_json(ACTUAL_PATH)

    g_rows = gold_rows(gold)
    a_rows = actual_rows(actual)

    actual_cas = {
        cas
        for row in a_rows
        for cas in row["cas"]
    }

    missing = []

    for g in g_rows:
        for cas in g["cas"]:

            if cas not in actual_cas:
                missing.append({
                    "page": g["page"],
                    "gold_row": g["row_number"],
                    "gold_source_row": g[
                        "row_index"
                    ],
                    "cas": cas,
                    "chemical": g["chemical"],
                })

    assert not missing, (
        "Gold CAS values missing from actual Layer 2:\n"
        + "\n".join(
            str(item)
            for item in missing
        )
    )


# ============================================================================
# GOLD CAS UNIQUENESS
# ============================================================================

def test_unique_gold_cas_identity():
    gold = load_json(GOLD_PATH)

    rows = gold_rows(gold)

    seen = {}

    for row in rows:

        identity = (
            row["page"],
            row["table_index"],
            row["row_index"],
        )

        for cas in row["cas"]:
            seen.setdefault(
                cas,
                []
            ).append(
                identity
            )

    duplicates = {
        cas: locations
        for cas, locations in seen.items()
        if len(set(locations)) > 1
    }

    assert not duplicates, (
        "CAS appears in multiple independent Gold rows:\n"
        + str(duplicates)
    )


def test_page_51_ammonia_group_is_three_logical_rows():
    actual = load_json(ACTUAL_PATH)
    rows = actual_rows(actual)
    page_51 = [row for row in rows if row["page"] == 51]

    by_cas = {}
    for row in page_51:
        for cas in row["cas"]:
            by_cas[cas] = row

    ammonia = by_cas.get("7664-41-7")
    chloride = by_cas.get("12125-02-9")
    dichromate = by_cas.get("7789-09-5")

    assert ammonia is not None, "missing Ammonia 7664-41-7"
    assert chloride is not None, "missing Ammonium chloride fume 12125-02-9"
    assert dichromate is not None, "missing Ammonium dichromate 7789-09-5"

    assert ammonia["cas"] == ["7664-41-7"]
    assert chloride["cas"] == ["12125-02-9"]
    assert dichromate["cas"] == ["7789-09-5"]

    ammonia_mw = _ascii_digits(ammonia.get("MW"))
    chloride_mw = _ascii_digits(chloride.get("MW"))
    dichromate_mw = _ascii_digits(dichromate.get("MW"))

    assert "17" in ammonia_mw and "03" in ammonia_mw, ammonia_mw
    assert "53" in chloride_mw and "50" in chloride_mw, chloride_mw
    assert "252" in dichromate_mw and "07" in dichromate_mw, dichromate_mw
    assert "252" not in ammonia_mw
    assert "17" not in chloride_mw
    assert "53" not in dichromate_mw