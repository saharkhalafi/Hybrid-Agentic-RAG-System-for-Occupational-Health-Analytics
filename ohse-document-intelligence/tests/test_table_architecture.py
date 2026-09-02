"""Architecture-level regression tests for table extraction."""

from __future__ import annotations

import json
from pathlib import Path

from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator
from pipeline_contracts.header_reconstruction import reconstruct_header_structure
from pipeline_contracts.numeric_integrity import (
    extract_ceiling_from_stel_c_cell,
    parse_numeric_cell,
    try_normalize,
    validate_row_number_cell,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_header_hierarchy_stel_twa_under_exposure_parent():
    rows = [
        [
            {"column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
            {"column": 1, "text": "نمادها"},
            {"column": 2, "text": "حد مجاز مواجهه شغلی TWA STEL/C", "source_reference": {"column_span": 2}},
            {"column": 3, "text": ""},
            {"column": 4, "text": "وزن ملکولی"},
            {"column": 5, "text": "نام علمی ماده شیمیایی"},
            {"column": 6, "text": "ردیف"},
        ],
        [
            {"column": 2, "text": "STEL/C"},
            {"column": 3, "text": "TWA"},
        ],
        [
            {"column": 0, "text": "تحریک"},
            {"column": 1, "text": "A4"},
            {"column": 2, "text": "2 ppm"},
            {"column": 3, "text": "1 ppm"},
            {"column": 4, "text": "89/14"},
            {"column": 5, "text": "2-Aminobutanol [96-20-8]"},
            {"column": 6, "text": "34"},
        ],
    ]
    structure = reconstruct_header_structure(rows, table_type="chemical_oel")
    by_col = {col.column_index: col for col in structure.physical_columns}

    assert by_col[2].semantic_field == "exposure_limit.stel_c"
    assert "STEL/C" in by_col[2].header_path
    assert by_col[3].semantic_field == "exposure_limit.twa"
    assert "TWA" in by_col[3].header_path
    assert by_col[2].parent_header == "حد مجاز مواجهه شغلی TWA STEL/C"
    assert structure.physical_column_count == 7


def test_numeric_preservation_plain_decimal():
    result = parse_numeric_cell("0.3 ppm", field_type="TWA")
    assert result.original_value == "0.3 ppm"
    assert result.parsed_token == "0.3"
    assert result.normalized_value == "0.3"
    assert result.numeric_parse_status == "VALIDATED"


def test_numeric_preservation_slash_original():
    result = parse_numeric_cell("۰/۳ mg/m³", field_type="TWA")
    assert result.original_value == "۰/۳ mg/m³"
    assert result.parsed_token == "۰/۳"
    assert result.normalized_value == "0.3"
    assert result.numeric_parse_method == "deterministic_persian_decimal"


def test_twa_rtl_slash_uses_normalized_value():
    result = parse_numeric_cell("(R) ³ mg/m\n05 /0", field_type="TWA")
    assert result.normalized_value == "0.05"

    result = parse_numeric_cell("mg/m³ 0000051 /0", field_type="TWA")
    assert result.normalized_value == "0.0000051"


def test_twa_ignores_cubic_metre_exponent_when_another_number_exists():
    result = parse_numeric_cell("3\nmg/m\n5", field_type="TWA")
    assert result.parsed_token == "5"
    assert result.normalized_value == "5"

    result = parse_numeric_cell("(IFV) 3 mg/m\n1", field_type="TWA")
    assert result.parsed_token == "1"
    assert result.normalized_value == "1"


def test_twa_keeps_lone_three_when_it_is_the_limit():
    result = parse_numeric_cell("3 mg/m³", field_type="TWA")
    assert result.parsed_token == "3"
    assert result.normalized_value == "3"

    result = parse_numeric_cell("3 mg/m3", field_type="STEL")
    assert result.parsed_token == "3"
    assert result.normalized_value == "3"


def test_limit_unit_only_mg_m3_is_not_an_exposure_value():
    for text in ("mg/m3", "mg/m^{3}", "mg/m3 (I)(E)"):
        result = parse_numeric_cell(text, field_type="STEL")
        assert result.parsed_token is None, text
        assert result.normalized_value is None, text


def test_ceiling_extracted_from_stel_c_cell():
    text = "3 ppm\nC 0/05 ppm"
    assert extract_ceiling_from_stel_c_cell(text) == "C 0/05 ppm"
    result = parse_numeric_cell(text, field_type="STEL")
    assert result.original_value == text
    assert result.parsed_token == "0/05"


def test_numeric_preservation_zero_slash_three():
    norm_val, method = try_normalize("0/3")
    assert norm_val == "0.3"
    assert method == "deterministic_persian_decimal"


def test_row_number_multi_value_rejected():
    issues = validate_row_number_cell("۷۴ ۷۵ ۷۶ ۷۷ ۷۸")
    assert len(issues) == 1
    assert "ROW_NUMBER_MULTI_VALUE" in issues[0]


def test_row_number_single_value_ok():
    assert validate_row_number_cell("74") == []
    assert validate_row_number_cell("۷۴") == []


def test_column_count_mismatch_triggers_review():
    rows = [
        [{"column": i, "text": t} for i, t in enumerate(["ردیف", "نام", "وزن", "حد", "نماد", "مبنای"])],
        [{"column": 5, "text": "Benzaldehyde [100-52-7]"}],
    ]
    structure = reconstruct_header_structure(rows, table_type="chemical_oel")
    assert any(i["type"] == "column_count_mismatch" for i in structure.issues)
    assert structure.header_structure_valid is False


def test_cas_derived_from_chemical_name_not_physical_column():
    rows = [
        [
            {"column": 0, "text": "مبنای تعیین حد"},
            {"column": 1, "text": "نمادها"},
            {"column": 2, "text": "حد مجاز مواجهه شغلی STEL/C TWA"},
            {"column": 3, "text": "وزن ملکولی"},
            {"column": 4, "text": "نام علمی ماده شیمیایی"},
            {"column": 5, "text": "ردیف"},
        ],
        [
            {"column": 4, "text": "Benzaldehyde [100-52-7]", "cell_id": "c1"},
            {"column": 5, "text": "10", "cell_id": "c2"},
        ],
    ]
    structure = reconstruct_header_structure(rows, table_type="chemical_oel")
    semantic_fields = {col.semantic_field for col in structure.physical_columns}
    assert "CAS" not in semantic_fields

    gold = TableGoldGenerator().generate(
        {
            "table_id": "table_test_01",
            "page_number": 1,
            "table_type": "chemical_oel",
            "rows": rows,
        }
    )
    row = gold["rows"][0]
    assert row["CAS"]["value"] == "100-52-7"
    assert row["CAS"]["cell_id"] == "c1"


def test_numeric_mismatch_detected_by_validator():
    evidence = [
        {
            "cell_id": "cell_x",
            "text": "0.3 ppm",
            "normalized_value": "0.3 ppm",
        }
    ]
    validator = GoldsetValidator(evidence)
    issues = validator.validate_table_field(
        "TWA",
        {
            "value": "30",
            "normalized_value": "30",
            "original_value": "0.3 ppm",
            "cell_id": "cell_x",
            "value_status": "extracted",
        },
    )
    assert any("NUMERIC_VALUE_MISMATCH" in i or "not in source cell" in i for i in issues)


def test_table_050_seven_physical_columns_from_evidence():
    path = PROJECT_ROOT / "data" / "evidence" / "2b0822424448_46-70" / "tables" / "table_050_01.json"
    if not path.exists():
        return
    table = json.loads(path.read_text(encoding="utf-8"))
    structure = reconstruct_header_structure(table["rows"], table_type="chemical_oel")
    assert structure.physical_column_count == 7
    mapping = structure.header_mapping
    assert mapping.get(5) == "chemical_name"
    assert mapping.get(6) == "row_number"
    assert "STEL" in mapping.values() or "TWA" in mapping.values()


def test_contaminated_header_detected_table_047():
    path = PROJECT_ROOT / "data" / "evidence" / "2b0822424448_46-70" / "tables" / "table_047_01.json"
    if not path.exists():
        return
    table = json.loads(path.read_text(encoding="utf-8"))
    structure = reconstruct_header_structure(table["rows"], table_type="chemical_oel")
    assert any(i["type"] == "contaminated_header_row" for i in structure.issues)
    assert structure.geometry_valid is False
