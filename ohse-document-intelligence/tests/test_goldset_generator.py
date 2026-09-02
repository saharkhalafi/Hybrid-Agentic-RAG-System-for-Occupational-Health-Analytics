"""Tests for goldset generator."""

from __future__ import annotations

from goldset_generator.metadata_generator import detect_document_type, generate_page_metadata
from goldset_generator.structural_resolver import _document_ai_table_quality_poor
from goldset_generator.document_processor import ExtractedCellRecord, ExtractedTableRecord
from goldset_generator.entity_extractor import EntityExtractor
from goldset_generator.knowledge_generator import KnowledgeGenerator
from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator


def test_detect_chemical_oel_table():
    tables = [{"table_type": "chemical_oel"}]
    assert detect_document_type("TWA STEL OEL limits", tables) == "chemical_oel_table"


def test_table_gold_chemical_mapping():
    table = {
        "table_id": "table_046_01",
        "page_number": 46,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {"cell_id": "cell_1_0_0", "column": 0, "text": "مبنای تعیین حد", "bbox": {"x": 1}},
                {"cell_id": "cell_1_0_1", "column": 1, "text": "TWA", "bbox": {"x": 2}},
            ],
            [
                {"cell_id": "cell_1_1_0", "column": 0, "text": "Acetaldehyde [75-07-0]", "bbox": {"x": 3}},
                {"cell_id": "cell_1_1_1", "column": 1, "text": "25 ppm", "bbox": {"x": 4}},
            ],
        ],
    }
    gold = TableGoldGenerator().generate(table)
    assert gold["table_type"] == "chemical_oel"
    assert len(gold["rows"]) >= 1


def test_merged_oel_header_expands_to_stel_and_twa_columns():
    table = {
        "table_id": "table_050_01",
        "page_number": 50,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {"cell_id": "h0", "column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
                {"cell_id": "h1", "column": 1, "text": "نمادها"},
                {"cell_id": "h2", "column": 2, "text": "حد مجاز مواجهه شغلی TWA STEL/C"},
                {"cell_id": "h3", "column": 3, "text": ""},
                {"cell_id": "h4", "column": 4, "text": "وزن ملکولی"},
                {"cell_id": "h5", "column": 5, "text": "نام علمی ماده شیمیایی"},
                {"cell_id": "h6", "column": 6, "text": "ردیف"},
            ],
            [
                {"cell_id": "s2", "column": 2, "text": "۲ ppm", "normalized_value": "2 ppm"},
                {"cell_id": "t3", "column": 3, "text": "1 ppm", "normalized_value": "1 ppm"},
                {"cell_id": "m4", "column": 4, "text": "۸۹/۱۴", "normalized_value": "89/14"},
                {"cell_id": "n5", "column": 5, "text": "2-Aminobutanol [96-20-8]"},
                {"cell_id": "r6", "column": 6, "text": "۳۴", "normalized_value": "34"},
            ],
        ],
    }

    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]

    assert gold["header_mapping"]["2"] == "STEL"
    assert gold["header_mapping"]["3"] == "TWA"
    assert row["STEL"]["value"] == "۲"
    assert row["STEL"]["cell_id"] == "s2"
    assert row["TWA"]["value"] == "1"
    assert row["TWA"]["cell_id"] == "t3"


def test_combined_oel_limit_cell_splits_stel_twa_and_ceiling():
    table = {
        "table_id": "table_046_01",
        "page_number": 46,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {"cell_id": "h0", "column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
                {"cell_id": "h1", "column": 1, "text": "نمادها"},
                {"cell_id": "h2", "column": 2, "text": "حد مجاز مواجهه شغلی TWA STEL/C"},
                {"cell_id": "h3", "column": 3, "text": "وزن ملکولی"},
                {"cell_id": "h4", "column": 4, "text": "نام علمی ماده شیمیایی"},
                {"cell_id": "h5", "column": 5, "text": "ردیف"},
            ],
            [
                {"cell_id": "e2", "column": 2, "text": "۱۵ ppm ۱۰ ppm C ۴۰ ppm"},
                {"cell_id": "m3", "column": 3, "text": "۶۰/۰۵", "normalized_value": "60/05"},
                {"cell_id": "n4", "column": 4, "text": "Acetic acid [64-19-7]"},
                {"cell_id": "r5", "column": 5, "text": "۵", "normalized_value": "5"},
            ],
        ],
    }

    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]

    assert gold["header_mapping"]["2"] == "TWA_STEL"
    assert row["STEL"]["value"] == "۱۵"
    assert row["TWA"]["value"] == "۱۰"
    assert row["ceiling"]["value"] == "۴۰"
    assert row["STEL"]["cell_id"] == row["TWA"]["cell_id"] == row["ceiling"]["cell_id"] == "e2"


def test_combined_oel_cell_with_ocr_corrupted_second_value_flags_review():
    """When Document AI OCRs a Persian digit as a LaTeX math command (e.g.
    ``\\cdot``, ``\\Delta``, ``\\tau``) instead of a real digit, one of the two
    merged TWA/STEL values becomes unparseable. That missing value must be
    reported as ``extraction_uncertain`` (so it is routed to human review),
    never as ``absent`` (which would silently drop real source data)."""
    table = {
        "table_id": "table_054_01",
        "page_number": 54,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {"cell_id": "h0", "column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
                {"cell_id": "h1", "column": 1, "text": "نمادها"},
                {"cell_id": "h2", "column": 2, "text": "حد مجاز مواجهه شغلی TWA STEL/C"},
                {"cell_id": "h3", "column": 3, "text": "وزن ملکولی"},
                {"cell_id": "h4", "column": 4, "text": "نام علمی ماده شیمیایی"},
                {"cell_id": "h5", "column": 5, "text": "ردیف"},
            ],
            [
                {"cell_id": "e2", "column": 2, "text": "1mg/m^{3} \\tau~mg/m^{3}"},
                {"cell_id": "m3", "column": 3, "text": "۱۱۶/۰۸", "normalized_value": "116/08"},
                {"cell_id": "n4", "column": 4, "text": "Azodicarbonamide [123-77-3]"},
                {"cell_id": "r5", "column": 5, "text": "۶۷", "normalized_value": "67"},
            ],
        ],
    }

    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]

    assert gold["header_mapping"]["2"] == "TWA_STEL"
    assert row["TWA"]["value"] == "1"
    assert row["TWA"]["value_status"] == "extracted"
    # STEL could not be parsed from the garbled "\tau" token, but the cell
    # clearly encodes a second value (a second "mg/m^{3}" unit marker), so it
    # must be flagged for review rather than reported as legitimately absent.
    assert row["STEL"]["value"] is None
    assert row["STEL"]["value_status"] == "extraction_uncertain"
    assert row["STEL"]["original_value"] == "1mg/m^{3} \\tau~mg/m^{3}"


def test_merged_three_cell_header_maps_twa_stel_without_phantom_columns():
    """Pages 140–145 often have only 3 header cells; fallback must not invent
    phantom STEL/TWA columns that duplicate the real exposure sub-header."""
    headers = [
        {"cell_id": "h2", "column": 2, "text": "حد مجا ز مواجهه\nشغلی \nنمادها \n مبنای تعیین حد"},
        {"cell_id": "h5", "column": 5, "text": "ردیف\nنام علمی ماده شیمیایی \n وزن"},
        {"cell_id": "h6", "column": 6, "text": "مجاز مواجهه \nTWA \nSTEL/C"},
    ]
    from goldset_generator.table_gold_generator import _build_header_mapping

    hm = _build_header_mapping(headers, "chemical_oel")
    assert hm.get(6) == "TWA_STEL"
    assert "STEL" not in hm.values() or hm.get(6) == "TWA_STEL"
    assert sum(v == "TWA" for v in hm.values()) == 0


def test_enrich_limits_from_merged_cell_without_valid_cas():
    """When OCR breaks CAS brackets, limits must still be recovered from the
    merged chemical cell (common on pages 140–145)."""
    from goldset_generator.oel_row_parser import enrich_oel_rows

    row = {
        "chemical_name": {
            "value": "Sulfometuron",
            "original_value": "2]\n-\n97\n-\n[74222\n methyl\n Sulfometuron\n 38/364\n (IFV)\n3\nmg/m\n 5\n-\n A4",
            "value_status": "extracted",
            "cell_id": "cell_x",
        }
    }
    enriched = enrich_oel_rows([row])
    assert enriched[0]["TWA"]["value"] == "3"
    assert enriched[0]["STEL"]["value"] == "5"


def test_validator_rejects_hallucinated_entity_value():
    evidence = [{"cell_id": "cell_1", "text": "25 ppm", "normalized_value": "25 ppm"}]
    validator = GoldsetValidator(evidence)
    issues = validator.validate_entity({"text": "CAS number", "type": "cas_number", "linked_cell_ids": ["cell_1"]})
    assert any("schema label" in issue for issue in issues)


def test_validator_rejects_cross_page_triple():
    evidence = [
        {"cell_id": "cell_table_046_01_1_0", "text": "75-07-0", "page_number": 46},
    ]
    validator = GoldsetValidator(evidence)
    triple = {
        "subject": "Acetaldehyde",
        "predicate": "has_CAS",
        "object": "75-07-0",
        "object_reference": {"cell_id": "cell_table_046_01_1_0"},
        "source_reference": {"cell_id": "cell_table_046_01_1_0", "cell_ids": ["cell_table_046_01_1_0"]},
    }
    issues = validator.validate_triple(triple, page_number=47)
    assert any("page 46" in issue for issue in issues)


def test_entity_extractor_uses_table_gold_values():
    table_gold = {
        "table_id": "table_047_01",
        "page_number": 47,
        "table_type": "chemical_oel",
        "rows": [
            {
                "persian_chemical_name": {
                    "value": "استونیتریل",
                    "cell_id": "cell_a",
                    "value_status": "extracted",
                    "bbox": {"x": 1},
                },
                "CAS": {
                    "value": "75-05-8",
                    "cell_id": "cell_b",
                    "value_status": "extracted",
                    "original_value": "Acetonitrile [75-05-8]",
                    "bbox": {"x": 2},
                },
                "TWA": {
                    "value": "20",
                    "unit": "ppm",
                    "cell_id": "cell_c",
                    "value_status": "extracted",
                    "original_value": "20 ppm",
                    "bbox": {"x": 3},
                },
            }
        ],
    }
    entities = EntityExtractor().extract_from_table_golds(47, [table_gold])
    cas_entities = [e for e in entities if e["type"] == "cas_number"]
    assert cas_entities[0]["resolved_value"] == "75-05-8"
    assert "text" not in cas_entities[0]
    assert cas_entities[0]["linked_cell_ids"] == ["cell_b"]


def test_knowledge_generator_triple_from_table_gold():
    table_gold = {
        "table_id": "table_047_01",
        "page_number": 47,
        "table_type": "chemical_oel",
        "rows": [
            {
                "persian_chemical_name": {
                    "value": "استونیتریل",
                    "cell_id": "cell_table_047_01_1_6",
                    "value_status": "extracted",
                },
                "CAS": {
                    "value": "75-05-8",
                    "cell_id": "cell_table_047_01_1_5",
                    "value_status": "extracted",
                    "original_value": "[75-05-8]",
                },
            }
        ],
    }
    from goldset_generator.knowledge_generator import KnowledgeGenerator

    triples = KnowledgeGenerator().generate_from_table_golds(47, [table_gold])
    cas_triple = next(t for t in triples if t["predicate"] == "has_CAS")
    assert cas_triple["object_reference"]["cell_id"] == "cell_table_047_01_1_5"
    resolved = KnowledgeGenerator().resolve_triples(
        triples,
        {
            "cell_table_047_01_1_5": {
                "cell_id": "cell_table_047_01_1_5",
                "text": "[75-05-8]",
                "page_number": 47,
            }
        },
    )
    assert resolved[0]["object"] == "75-05-8"


def test_validator_final_confidence_is_minimum():
    validator = GoldsetValidator([])
    conf = validator.compute_final_confidence(
        ocr_confidence=0.95,
        structural_confidence=0.7,
        semantic_confidence=0.99,
    )
    assert conf == 0.7


def test_page_metadata():
    meta = generate_page_metadata(44, "فهرست مطالب introduction", [])
    assert meta["document_type"] in {"introduction", "unknown"}


def _cell(
    text: str,
    row: int,
    col: int,
    *,
    table_id: str = "table_t_01",
    page_number: int = 48,
) -> ExtractedCellRecord:
    return ExtractedCellRecord(
        cell_id=f"cell_{table_id}_{row}_{col}",
        table_id=table_id,
        page_number=page_number,
        row=row,
        column=col,
        text=text,
        bbox=None,
        confidence=0.9,
        bbox_confidence=None,
        bbox_source=None,
        source="document_ai",
    )


def test_document_ai_quality_poor_detects_header_cas_pollution():
    """Header row must not contain multiple CAS — signals transposed DA structure."""
    page_text = "Acrylamide [79-06-1] Acrylic acid [79-10-7] Acrylonitrile [107-13-1]"
    header = [
        _cell("TWA", 0, 0),
        _cell("Acrylamide [79-06-1] Acrylic acid [79-10-7]", 0, 1),
    ]
    data = [[_cell("", 1, 0), _cell("", 1, 1)]]
    table = ExtractedTableRecord(
        table_id="table_048_01",
        page_number=48,
        table_type="chemical_oel",
        rows=[header, *data],
        structural_confidence=0.5,
        raw_markdown="",
    )
    assert _document_ai_table_quality_poor(page_text, table) is True


def test_document_ai_quality_good_when_cas_in_data_rows():
    page_text = "Acetaldehyde [75-07-0] Acetone [67-64-1]"
    header = [
        _cell("مبنای تعیین حد مجاز مواجهه", 0, 0, table_id="table_046_01"),
        _cell("نمادها", 0, 1, table_id="table_046_01"),
        _cell("STEL/C", 0, 2, table_id="table_046_01"),
        _cell("TWA", 0, 3, table_id="table_046_01"),
        _cell("وزن ملکولی", 0, 4, table_id="table_046_01"),
        _cell("نام علمی ماده شیمیایی", 0, 5, table_id="table_046_01"),
        _cell("ردیف", 0, 6, table_id="table_046_01"),
    ]
    data = [
        [
            _cell("", 1, 0, table_id="table_046_01"),
            _cell("A4", 1, 1, table_id="table_046_01"),
            _cell("", 1, 2, table_id="table_046_01"),
            _cell("25 ppm", 1, 3, table_id="table_046_01"),
            _cell("44/05", 1, 4, table_id="table_046_01"),
            _cell("Acetaldehyde [75-07-0]", 1, 5, table_id="table_046_01"),
            _cell("2", 1, 6, table_id="table_046_01"),
        ],
        [
            _cell("", 2, 0, table_id="table_046_01"),
            _cell("A4", 2, 1, table_id="table_046_01"),
            _cell("", 2, 2, table_id="table_046_01"),
            _cell("500 ppm", 2, 3, table_id="table_046_01"),
            _cell("58/08", 2, 4, table_id="table_046_01"),
            _cell("Acetone [67-64-1]", 2, 5, table_id="table_046_01"),
            _cell("3", 2, 6, table_id="table_046_01"),
        ],
    ]
    table = ExtractedTableRecord(
        table_id="table_046_01",
        page_number=46,
        table_type="chemical_oel",
        rows=[header, *data],
        structural_confidence=0.9,
        raw_markdown="",
    )
    assert _document_ai_table_quality_poor(page_text, table) is False


def test_validation_engine_merged_cell_requires_review():
    from validation_engine.engine import ValidationEngine

    engine = ValidationEngine()
    row = {
        "chemical_name": {
            "value": None,
            "value_status": "merged_cell",
            "cell_id": "cell_x",
            "original_value": "Acrylamide [79-06-1] 0.03 mg/m3",
        }
    }
    result = engine.validate_table_row("chemical_oel", row, schema_id="chemical_oel_v1")
    assert result.requires_review is True
    assert any(i.code.value == "MERGED_CELL_UNRESOLVED" for i in result.issues)


def test_validation_engine_flags_ocr_corrupted_stel_for_review():
    """End-to-end: a merged TWA/STEL cell whose second value was OCR-garbled
    into a LaTeX token must make it all the way to ValidationEngine as an
    EXTRACTION_UNCERTAIN issue requiring review — not silently pass through."""
    from validation_engine.engine import ValidationEngine

    table = {
        "table_id": "table_054_01",
        "page_number": 54,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {"cell_id": "h0", "column": 0, "text": "مبنای تعیین حد مجاز مواجهه"},
                {"cell_id": "h1", "column": 1, "text": "نمادها"},
                {"cell_id": "h2", "column": 2, "text": "حد مجاز مواجهه شغلی TWA STEL/C"},
                {"cell_id": "h3", "column": 3, "text": "وزن ملکولی"},
                {"cell_id": "h4", "column": 4, "text": "نام علمی ماده شیمیایی"},
                {"cell_id": "h5", "column": 5, "text": "ردیف"},
            ],
            [
                {"cell_id": "e2", "column": 2, "text": "1mg/m^{3} \\tau~mg/m^{3}"},
                {"cell_id": "m3", "column": 3, "text": "۱۱۶/۰۸", "normalized_value": "116/08"},
                {"cell_id": "n4", "column": 4, "text": "Azodicarbonamide [123-77-3]"},
                {"cell_id": "r5", "column": 5, "text": "۶۷", "normalized_value": "67"},
            ],
        ],
    }
    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]

    engine = ValidationEngine()
    result = engine.validate_table_row("chemical_oel", row, schema_id="chemical_oel_v1")
    assert result.requires_review is True
    assert any(
        i.code.value == "EXTRACTION_UNCERTAIN" and i.field_name == "STEL" for i in result.issues
    )


def test_parse_molecular_weight_rtl_visual_order():
    from goldset_generator.oel_row_parser import (
        parse_layer2_molecular_weight,
        parse_molecular_weight,
    )

    assert parse_layer2_molecular_weight("08 / 71") == "71.08"
    assert parse_layer2_molecular_weight("06 / 72") == "72.06"
    assert parse_layer2_molecular_weight("05 / 53") == "53.05"
    assert parse_layer2_molecular_weight("14 / 146") == "146.14"
    assert parse_layer2_molecular_weight("10 / 108") == "108.10"
    assert parse_layer2_molecular_weight("8/ 269") == "269.8"
    assert parse_layer2_molecular_weight("26 / 190") == "190.26"
    assert parse_molecular_weight("183/16") == "183.16"
    assert parse_molecular_weight("222/68") == "222.68"
    assert parse_molecular_weight("41/05") == "41.05"
    assert parse_layer2_molecular_weight("183/16") is None
    assert parse_layer2_molecular_weight("41/05") is None
    assert parse_layer2_molecular_weight("26/98") is None


def test_oel_row_parser_page_47_merged_cell():
    from goldset_generator.oel_row_parser import enrich_oel_rows, split_chemical_segments

    merged_text = (
        "Acetone cyanohydrin [75-86-5] ,as CN \n10\n/\n85\n \n-\n \n3\nmg/m\n  5 \nC\n  پوست"
    )
    segments = split_chemical_segments(merged_text)
    assert len(segments) == 1
    assert segments[0]["CAS"] == "75-86-5"
    assert segments[0]["molecular_weight"] == "85.10"
    assert segments[0]["symbols"] == "پوست"

    acetophenone = "Acetophenone [98-86-2 ] \n15\n/\n120\n \nppm\n \n10\n \n-\n \n-"
    segments = split_chemical_segments(acetophenone)
    assert segments[0]["molecular_weight"] == "120.15"
    assert segments[0]["TWA"] == "10"
    assert segments[0]["TWA_unit"] == "ppm"

    fluorene = "2-Acetylamino flourene [53-96-3 ] \n27\n/\n223\n \nppm\n 1\n \n-\n \n-\n تحریک و سوزش چشم"
    segments = split_chemical_segments(fluorene)
    assert segments[0]["molecular_weight"] == "223.27"
    assert segments[0]["TWA"] == "1"
    assert segments[0]["TWA_unit"] == "ppm"

    aspirin = "Acetylsalicylic acid [50-78-2] \n15\n/\n180\n \n3\nmg/m\n 3/0\n \n-"
    segments = split_chemical_segments(aspirin)
    assert segments[0]["TWA"] == "0.3"
    assert segments[0]["TWA_unit"] == "mg/m³"

    acrolein = "Acrolein [107-02-8] \n06\n/\n56\n \n-\n \nppm\n \n05\n/0 \nC\n \n پوست؛A3\n سوزش چشم"
    segments = split_chemical_segments(acrolein)
    assert segments[0]["CAS"] == "107-02-8"
    assert segments[0]["ceiling"] == "0.05"
    assert segments[0]["TWA"] is None
    assert segments[0]["symbols"] == "پوست؛ A3"

    dacarbazine = (
        "Dacarbazine [4342-03-4] \n18\n/\n182\n \n³\nmg/m\n \n0009\n/0\n \n-\n \nA3\n \n-"
    )
    segments = split_chemical_segments(dacarbazine)
    assert segments[0]["TWA"] == "0.0009"
    assert segments[0]["TWA_unit"] == "mg/m³"

    styrene_ceiling = (
        "Styrene [100-42-5] \n15\n/\n120\n \nppm\n 1\n \n-\n \nDSEN\n \n00006\n/0  \nC"
    )
    segments = split_chemical_segments(styrene_ceiling)
    assert segments[0]["ceiling"] == "0.00006"
    assert segments[0]["ceiling_unit"] == "ppm"

    silica = "Crystalline silica [14808-60-7] \n09\n/\n60\n \n(R)\n 3\nmg/m\n \n025\n/0\n \n-\n \nA2"
    segments = split_chemical_segments(silica)
    assert segments[0]["TWA"] == "0.025"
    assert segments[0]["TWA_unit"] == "mg/m³"

    from goldset_generator.oel_row_parser import extract_persian_from_row_number

    assert extract_persian_from_row_number("11\n \n2\n- استیل آمینو فلورن") == "2- استیل آمینو فلورن"
    assert "تترا" in (extract_persian_from_row_number("13\n \n2،2،1،1\n-تترا بر\nومو اتان") or "")
    aspirin_rn = "13\n \n2،2،1،1\n-تترا بر\nومو اتان\n)اسید استیل سالیسیلیک (آسپیرین"
    assert "آسپیرین" in (extract_persian_from_row_number(aspirin_rn, index=1) or "")
    assert "آکرول" in (
        extract_persian_from_row_number(")اسید استیل سالیسیلیک (آسپیرین\n14\n آکرولئین", index=-1) or ""
    )

    multi_text = (
        "1,1,2,2-Tetrabromoethane/[79-27-6] \n70\n/\n345\n \n0/1 \nppm\n \n-\n \n-\n"
        ")اسید استیل سالیسیلیک (آسپیرین\nAcetylsalicylic acid [50-78-2] \n15\n/\n180\n \n3\nmg/m\n 3/0\n \n-"
    )
    segments = split_chemical_segments(multi_text)
    assert len(segments) == 2
    assert segments[0]["CAS"] == "79-27-6"
    assert segments[1]["CAS"] == "50-78-2"

    acrolein_text = (
        ")اسید استیل سالیسیلیک (آسپیرین\nAcetylsalicylic acid [50-78-2] \n15\n/\n180\n \n3\nmg/m\n 3/0\n \n-"
        "Acrolein [107-02-8] \n06\n/\n56\n \n-\n \nppm\n \n05\n/0 \nC\n \n پوست؛A3\n سوزش چشم"
    )
    segments = split_chemical_segments(acrolein_text)
    assert len(segments) == 2
    assert segments[1]["CAS"] == "107-02-8"
    assert segments[1]["symbols"] == "پوست؛ A3"

    rows = enrich_oel_rows(
        [
            {
                "row_number": {"value": "14", "value_status": "extracted"},
                "persian_chemical_name": {"value": "آکرولئین", "value_status": "extracted"},
                "chemical_name": {
                    "value": None,
                    "value_status": "merged_cell",
                    "original_value": acrolein_text,
                    "cell_id": "cell_x",
                },
            }
        ]
    )
    assert len(rows) == 2
    assert rows[1]["CAS"]["value"] == "107-02-8"


def test_header_mapping_detects_ambiguity():
    from goldset_generator.header_mapping import analyze_header_mapping

    meta = analyze_header_mapping({0: "symbols", 1: "symbols", 3: "TWA", 6: "TWA"})
    assert meta["mapping_status"] == "review_required"
    assert any(i["type"] == "ambiguous_header_mapping" for i in meta["mapping_issues"])


def test_schema_registry_loads_chemical_oel():
    from schema_registry.registry import get_schema_registry

    registry = get_schema_registry()
    schema = registry.get("chemical_oel_v1")
    assert schema is not None
    assert "CAS" in registry.field_names("chemical_oel_v1")
    table = {
        "table_id": "table_048_01",
        "page_number": 48,
        "table_type": "chemical_oel",
        "rows": [
            [
                {"cell_id": "h0", "column": 5, "text": "نام علمی ماده شیمیایی"},
            ],
            [
                {
                    "cell_id": "merged",
                    "column": 5,
                    "text": "Acrylamide [79-06-1] 0/03 mg/m3",
                    "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
                },
                {"cell_id": "sym", "column": 1, "text": "DSEN ؛BEI", "bbox": {"x": 5, "y": 6, "width": 7, "height": 8}},
                {"cell_id": "fa", "column": 6, "text": "15\n آکریل آمید", "bbox": {"x": 9, "y": 10, "width": 11, "height": 12}},
            ],
        ],
    }
    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]
    assert row["chemical_name"]["value"] == "Acrylamide"
    assert row["CAS"]["value"] == "79-06-1"
    assert row["symbols"]["value"] == "DSEN ؛BEI"
    assert row["persian_chemical_name"]["value"] == "آکریل آمید"
    assert row["row_number"]["value"] == "15"


def _oel_six_column_table(rows: list[list[ExtractedCellRecord]]) -> ExtractedTableRecord:
    return ExtractedTableRecord(
        table_id="table_046_01",
        page_number=46,
        table_type="chemical_oel",
        rows=rows,
        structural_confidence=0.9,
        raw_markdown="",
    )


def test_expand_oel_merged_limit_header_to_seven_columns():
    from goldset_generator.structural_resolver import _expand_oel_limit_columns

    table = _oel_six_column_table(
        [
            [
                _cell("مبنای تعیین حد مجاز مواجهه", 0, 0),
                _cell("نمادها", 0, 1),
                _cell("حد مجاز مواجهه شغلی TWA STEL/C", 0, 2),
                _cell("وزن ملکولی", 0, 3),
                _cell("نام علمی ماده شیمیایی", 0, 4),
                _cell("ردیف", 0, 5),
            ],
            [
                _cell("effect", 1, 0),
                _cell("A2", 1, 1),
                _cell("۱۵ ppm ۱۰ ppm", 1, 2),
                _cell("۶۰/۰۵", 1, 3),
                _cell("Acetic acid [64-19-7]", 1, 4),
                _cell("۵", 1, 5),
            ],
        ]
    )

    expanded, count = _expand_oel_limit_columns(table)

    assert count == 1
    assert max(len(row) for row in expanded.rows) == 7
    assert expanded.rows[0][2].text == "حد مجاز مواجهه شغلی STEL/C"
    assert expanded.rows[0][3].text == "TWA"
    assert expanded.rows[1][2].text == "۱۵ ppm"
    assert expanded.rows[1][3].text == "۱۰ ppm"
    assert expanded.rows[1][4].text == "۶۰/۰۵"
    assert expanded.rows[1][5].text == "Acetic acid [64-19-7]"


def test_expand_oel_limit_columns_leaves_seven_column_table_unchanged():
    from goldset_generator.structural_resolver import _expand_oel_limit_columns

    table = _oel_six_column_table(
        [
            [
                _cell("مبنای تعیین حد مجاز مواجهه", 0, 0),
                _cell("نمادها", 0, 1),
                _cell("حد مجاز مواجهه شغلی STEL/C", 0, 2),
                _cell("TWA", 0, 3),
                _cell("وزن ملکولی", 0, 4),
                _cell("نام علمی ماده شیمیایی", 0, 5),
                _cell("ردیف", 0, 6),
            ],
            [
                _cell("effect", 1, 0),
                _cell("A4", 1, 1),
                _cell("", 1, 2),
                _cell("1mg/m3", 1, 3),
                _cell("Aluminum metal [7429-90-5]", 1, 5),
                _cell("۳۲", 1, 6),
            ],
        ]
    )

    expanded, count = _expand_oel_limit_columns(table)

    assert count == 0
    assert expanded is table


def test_expand_oel_limit_columns_skips_non_oel_table():
    from goldset_generator.structural_resolver import _expand_oel_limit_columns

    table = ExtractedTableRecord(
        table_id="table_noise_01",
        page_number=240,
        table_type="noise_limits",
        rows=[[_cell("Level", 0, 0), _cell("dB", 0, 1)]],
        structural_confidence=0.9,
        raw_markdown="",
    )

    expanded, count = _expand_oel_limit_columns(table)

    assert count == 0
    assert expanded is table


def test_expand_oel_basis_symbols_column_page_51():
    from goldset_generator.structural_resolver import (
        _expand_oel_basis_symbols_column,
    )

    table = ExtractedTableRecord(
        table_id="table_051_01",
        page_number=51,
        table_type="chemical_oel",
        structural_confidence=0.9333,
        raw_markdown="",
        rows=[
            [
                ExtractedCellRecord(
                    cell_id="h0",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=0,
                    text="مبنای تعیین حد نمادها مجاز مواجهه",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
                ExtractedCellRecord(
                    cell_id="h1",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=1,
                    text="حد مجاز مواجهه شغلی STEL/C",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
                ExtractedCellRecord(
                    cell_id="h2",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=2,
                    text="TWA",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
                ExtractedCellRecord(
                    cell_id="h3",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=3,
                    text="وزن ملکولی",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
                ExtractedCellRecord(
                    cell_id="h4",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=4,
                    text="نام علمی ماده شیمیایی",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
                ExtractedCellRecord(
                    cell_id="h5",
                    table_id="table_051_01",
                    page_number=51,
                    row=0,
                    column=5,
                    text="ردیف",
                    bbox=None,
                    confidence=None,
                    bbox_confidence=None,
                    bbox_source=None,
                    source="document_ai",
                ),
            ],
        ],
    )

    expanded, count = _expand_oel_basis_symbols_column(table)

    assert count == 1
    assert len(expanded.rows[0]) == 7

    cells = {
        cell.column: cell
        for cell in expanded.rows[0]
    }

    assert cells[0].text == "مبنای تعیین حد مجاز مواجهه"
    assert cells[1].text == "نمادها"
    assert cells[2].text == "حد مجاز مواجهه شغلی STEL/C"
    assert cells[3].text == "TWA"
    assert cells[4].text == "وزن ملکولی"
    assert cells[5].text == "نام علمی ماده شیمیایی"
    assert cells[6].text == "ردیف"


def test_pdf_words_assign_stel_left_and_twa_right_without_swapping():
    from ingestion.table_recovery import WordToken, recover_stel_twa_from_words

    page_width = 694.8
    swapped_dai_order = [
        WordToken("20", 316.2, 170.0, 324.6, 182.0, 47),
        WordToken("ppm", 327.2, 170.0, 345.0, 182.0, 47),
        WordToken("40ppm", 250.6, 170.0, 276.7, 182.0, 47),
        WordToken("41", 381.3, 170.0, 389.6, 182.0, 47),
    ]
    recovered = recover_stel_twa_from_words(swapped_dai_order, page_width, 165.0, 185.0)
    assert recovered["STEL"] is not None and "40" in recovered["STEL"]
    assert recovered["TWA"] is not None and "20" in recovered["TWA"]
    assert "40" not in (recovered["TWA"] or "")
    assert "20" not in (recovered["STEL"] or "")

    already_correct = [
        WordToken("2", 252.9, 200.0, 257.0, 212.0, 50),
        WordToken("ppm", 257.1, 200.0, 274.9, 212.0, 50),
        WordToken("1", 318.3, 200.0, 322.4, 212.0, 50),
        WordToken("ppm", 325.0, 200.0, 342.8, 212.0, 50),
    ]
    recovered = recover_stel_twa_from_words(already_correct, page_width, 195.0, 215.0)
    assert recovered["STEL"] is not None and "2" in recovered["STEL"]
    assert recovered["TWA"] is not None and "1" in recovered["TWA"]
    assert "1" not in recovered["STEL"].replace("ppm", "")


def test_pdf_limit_band_ignores_molecular_weight_x():
    from ingestion.table_recovery import WordToken, recover_stel_twa_from_words

    page_width = 694.8
    words = [
        WordToken("1", 318.5, 250.0, 322.6, 262.0, 49),
        WordToken("ppm", 324.8, 250.0, 342.6, 262.0, 49),
        WordToken("76", 381.3, 250.0, 389.6, 262.0, 49),
        WordToken("/", 389.7, 250.0, 392.3, 262.0, 49),
        WordToken("50", 392.4, 250.0, 400.5, 262.0, 49),
    ]
    recovered = recover_stel_twa_from_words(words, page_width, 245.0, 265.0)
    assert recovered["TWA"] is not None and "1" in recovered["TWA"]
    assert recovered["STEL"] is None
    assert "76" not in (recovered["TWA"] or "")
    assert "50" not in (recovered["TWA"] or "")


def test_cloned_limit_bboxes_need_pdf_x_assignment():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    cloned = {"x": 327.17, "y": 167.57, "width": 17.83, "height": 11.03}
    row = [
        {"column": 2, "text": "20 ppm", "bbox": cloned},
        {"column": 3, "text": "40ppm", "bbox": dict(cloned)},
        {
            "column": 5,
            "text": "Acetonitrile",
            "bbox": {"x": 465.7, "y": 178.4, "width": 34.5, "height": 10.0},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == {"STEL", "TWA"}


def test_distinct_parsed_stel_twa_do_not_need_pdf_overlay():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    row = [
        {
            "column": 2,
            "text": "2 ppm",
            "bbox": {"x": 244.0, "y": 200.0, "width": 30.0, "height": 11.0},
        },
        {
            "column": 3,
            "text": "1 ppm",
            "bbox": {"x": 317.0, "y": 200.0, "width": 30.0, "height": 11.0},
        },
        {
            "column": 4,
            "text": "58.08",
            "bbox": {"x": 379.0, "y": 200.0, "width": 23.0, "height": 12.0},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == set()


def test_spanned_stel_bbox_and_twa_column_park_need_pdf_x_assignment():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    spanned = [
        {
            "column": 2,
            "text": "ppm 1",
            "bbox": {"x": 258.17, "y": 250.75, "width": 64.47, "height": 15.63},
        },
        {"column": 3, "text": "", "bbox": None},
    ]
    assert "STEL" in pdf_limit_replace_fields(spanned, page_width)
    assert "TWA" in pdf_limit_replace_fields(spanned, page_width)

    parked_in_twa_x = [
        {
            "column": 2,
            "text": "0.1 f/cc",
            "bbox": {"x": 340.49, "y": 293.1, "width": 7.92, "height": 7.17},
        },
        {"column": 3, "text": "", "bbox": None},
    ]
    assert pdf_limit_replace_fields(parked_in_twa_x, page_width) == {"STEL", "TWA"}


def test_unreadable_ocr_limit_needs_pdf_x_assignment():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    row = [
        {"column": 2, "text": r"rr~mg/m^{3(l)}", "bbox": None},
        {"column": 3, "text": r"4~mg/m^{3(l)}", "bbox": None},
        {
            "column": 4,
            "text": "223.25",
            "bbox": {"x": 379.27, "y": 264.04, "width": 23.31, "height": 12.66},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == {"STEL", "TWA"}


def test_empty_stel_and_twa_need_pdf_x_assignment():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    row = [
        {"column": 2, "text": "", "bbox": None},
        {"column": 3, "text": None, "bbox": None},
        {
            "column": 4,
            "text": "84.08",
            "bbox": {"x": 381.31, "y": 161.90, "width": 19.23, "height": 12.66},
        },
        {
            "column": 6,
            "text": "41",
            "bbox": {"x": 594.22, "y": 161.90, "width": 8.19, "height": 12.66},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == {"STEL", "TWA"}


def test_twa_bbox_parked_in_mw_column_needs_pdf_x_assignment():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    row = [
        {"column": 2, "text": "", "bbox": None},
        {
            "column": 3,
            "text": "20 mg/m3",
            "bbox": {"x": 391.75, "y": 128.75, "width": 2.63, "height": 12.66},
        },
        {
            "column": 4,
            "text": "106 / 12",
            "bbox": {"x": 379.27, "y": 128.75, "width": 23.31, "height": 12.66},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == {"STEL", "TWA"}


def test_valid_stel_with_empty_twa_does_not_need_pdf_overlay():
    from goldset_generator.table_gold_generator import pdf_limit_replace_fields

    page_width = 694.8
    row = [
        {"column": 2, "text": "C0.05 ppm", "bbox": None},
        {"column": 3, "text": "", "bbox": None},
        {
            "column": 4,
            "text": "56.06",
            "bbox": {"x": 391.75, "y": 352.51, "width": 2.63, "height": 12.66},
        },
    ]
    assert pdf_limit_replace_fields(row, page_width) == set()


def test_pdf_words_recover_empty_dai_row_and_mw_spilled_twa():
    from ingestion.table_recovery import WordToken, recover_stel_twa_from_words
    from pipeline_contracts.numeric_integrity import parse_numeric_cell

    page_width = 694.8
    empty_dai_row = [
        WordToken("-", 261.8, 162.5, 266.0, 175.2, 51),
        WordToken("2/0", 310.7, 162.5, 321.8, 175.2, 51),
        WordToken("mg/m", 323.7, 159.5, 347.0, 170.6, 51),
        WordToken("84", 381.3, 161.9, 389.6, 174.6, 51),
        WordToken("41", 594.2, 161.9, 602.4, 174.6, 51),
    ]
    recovered = recover_stel_twa_from_words(empty_dai_row, page_width, 156.9, 179.6)
    assert recovered["STEL"] is not None and recovered["STEL"].strip() in {"-", "—", "–"}
    twa = parse_numeric_cell(recovered["TWA"] or "", field_type="TWA")
    assert twa.normalized_value == "0.2"

    spilled = [
        WordToken("20", 246.5, 129.3, 254.7, 142.0, 55),
        WordToken("mg/m", 254.8, 126.4, 278.2, 137.4, 55),
        WordToken("5", 314.0, 129.3, 318.1, 142.0, 55),
        WordToken("mg/m", 320.8, 126.4, 343.9, 137.4, 55),
        WordToken("106", 379.3, 128.8, 391.7, 141.4, 55),
        WordToken("73", 594.2, 128.8, 602.4, 141.4, 55),
    ]
    recovered = recover_stel_twa_from_words(spilled, page_width, 123.8, 146.4)
    stel = parse_numeric_cell(recovered["STEL"] or "", field_type="STEL")
    twa = parse_numeric_cell(recovered["TWA"] or "", field_type="TWA")
    assert stel.normalized_value == "20"
    assert twa.normalized_value == "5"


def test_pdf_row_identity_rejects_neighbor_y_band():
    from goldset_generator.table_gold_generator import pdf_row_identity_matches
    from ingestion.table_recovery import WordToken

    page_width = 694.8
    words = [
        WordToken("46", 594.2, 332.2, 602.4, 344.9, 51),
        WordToken("01", 315.5, 332.8, 323.8, 345.5, 51),
        WordToken("/0", 308.7, 332.8, 315.5, 345.5, 51),
    ]
    assert pdf_row_identity_matches(words, page_width, 327.2, 349.9, 46)
    assert not pdf_row_identity_matches(words, page_width, 327.2, 349.9, 48)
    assert not pdf_row_identity_matches(words, page_width, 327.2, 349.9, None)


def test_unique_pdf_row_number_relocates_y_span():
    from goldset_generator.table_gold_generator import pdf_unique_row_number_y_span
    from ingestion.table_recovery import WordToken, recover_stel_twa_from_words
    from pipeline_contracts.numeric_integrity import parse_numeric_cell

    page_width = 694.8
    words = [
        WordToken("46", 594.2, 332.2, 602.4, 344.9, 51),
        WordToken("01", 315.5, 332.8, 323.8, 345.5, 51),
        WordToken("/0", 308.7, 332.8, 315.5, 345.5, 51),
        WordToken("48", 594.2, 397.0, 602.4, 409.8, 51),
        WordToken("1/0", 314.0, 398.5, 324.5, 411.2, 51),
        WordToken("mg/m", 323.7, 395.5, 347.0, 406.6, 51),
        WordToken("-", 261.8, 398.5, 266.0, 411.2, 51),
    ]
    neighbor_span = pdf_unique_row_number_y_span(words, page_width, 48)
    assert neighbor_span is not None
    recovered = recover_stel_twa_from_words(words, page_width, neighbor_span[0], neighbor_span[1])
    twa = parse_numeric_cell(recovered["TWA"] or "", field_type="TWA")
    assert twa.normalized_value == "0.1"
    assert pdf_unique_row_number_y_span(words, page_width, 47) is None
    duplicated = words + [WordToken("48", 594.2, 450.0, 602.4, 462.8, 51)]
    assert pdf_unique_row_number_y_span(duplicated, page_width, 48) is None
    assert pdf_unique_row_number_y_span(words, page_width, None) is None


def test_unreadable_stel_in_neighbor_band_uses_row_number_anchor():
    from goldset_generator.table_gold_generator import pdf_unique_row_number_y_span
    from ingestion.table_recovery import WordToken, recover_stel_twa_from_words
    from pipeline_contracts.numeric_integrity import parse_numeric_cell

    page_width = 694.8
    words = [
        WordToken("63", 594.2, 360.8, 602.4, 373.5, 53),
        WordToken("5/0", 310.5, 361.5, 321.0, 374.2, 53),
        WordToken("62", 594.2, 325.6, 602.4, 338.4, 53),
        WordToken("-", 261.8, 326.5, 266.0, 339.2, 53),
        WordToken("10", 314.0, 326.5, 324.0, 339.2, 53),
        WordToken("mg/m", 325.0, 323.5, 348.0, 334.6, 53),
    ]
    span = pdf_unique_row_number_y_span(words, page_width, 62)
    assert span is not None
    recovered = recover_stel_twa_from_words(words, page_width, span[0], span[1])
    twa = parse_numeric_cell(recovered["TWA"] or "", field_type="TWA")
    assert twa.normalized_value == "10"
    assert recovered["STEL"] is not None and recovered["STEL"].strip() in {"-", "—", "–"}


def test_pdf_overrides_dai_only_when_same_numbers_are_column_swapped():
    from decimal import Decimal

    from goldset_generator.table_gold_generator import pdf_overrides_complete_dai_limits

    assert pdf_overrides_complete_dai_limits(
        Decimal("20"), Decimal("40"), Decimal("40"), Decimal("20")
    )
    assert not pdf_overrides_complete_dai_limits(
        Decimal("2"), Decimal("6"), None, Decimal("0.5")
    )
    assert not pdf_overrides_complete_dai_limits(
        Decimal("2"), Decimal("1"), Decimal("0.2"), Decimal("0.1")
    )
    assert pdf_overrides_complete_dai_limits(None, Decimal("1"), Decimal("2"), Decimal("1"))
