

from __future__ import annotations

import json
from pathlib import Path

import pytest

from goldset_generator.chemical_entity_extractor import (
    extract_chemical_entities_from_cell,
)
from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator
from ingestion.table_recovery import recover_chemical_oel_table


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PDF_PATH = PROJECT_ROOT.parent / "OHE6.pdf"


def test_multiple_cas_in_one_physical_cell():
    """One physical cell may contain multiple CAS entities with provenance."""

    cell = {
        "cell_id": "cell_table_046_01_1_5",
        "text": "اسفات\n[115096-11-2] [30560-19-1]; Acephate",
        "bbox": {
            "x": 420.0,
            "y": 138.0,
            "width": 154.0,
            "height": 24.0,
        },
        "source_reference": {
            "source_words": [
                {
                    "word": "[115096-11-2]",
                    "bbox": {
                        "x": 480,
                        "y": 140,
                        "width": 34,
                        "height": 10,
                    },
                    "source_span": "480,140-514,150",
                },
                {
                    "word": "[30560-19-1]",
                    "bbox": {
                        "x": 520,
                        "y": 140,
                        "width": 34,
                        "height": 10,
                    },
                    "source_span": "520,140-554,150",
                },
                {
                    "word": "Acephate",
                    "bbox": {
                        "x": 560,
                        "y": 140,
                        "width": 30,
                        "height": 10,
                    },
                    "source_span": "560,140-590,150",
                },
            ]
        },
    }

    entities = extract_chemical_entities_from_cell(cell)

    assert len(entities) == 1

    entity = entities[0]

    assert entity.chemical_name == "Acephate"
    assert len(entity.cas_numbers) == 2
    assert {c.value for c in entity.cas_numbers} == {
        "115096-11-2",
        "30560-19-1",
    }

    for cas in entity.cas_numbers:
        assert cas.source_bbox is not None
        assert cas.source_text.startswith("[")

    table = {
        "table_id": "table_046_01",
        "page_number": 46,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            [
                {
                    "cell_id": "h0",
                    "column": i,
                    "text": f"h{i}",
                }
                for i in range(7)
            ],
            [
                {
                    "cell_id": "c0",
                    "column": 0,
                    "text": "-",
                },
                {
                    "cell_id": "c1",
                    "column": 1,
                    "text": "-",
                },
                {
                    "cell_id": "c2",
                    "column": 2,
                    "text": "-",
                },
                {
                    "cell_id": "c3",
                    "column": 3,
                    "text": "3/0",
                },
                {
                    "cell_id": "c4",
                    "column": 4,
                    "text": "16/183",
                },
                cell
                | {
                    "column": 5,
                    "source_reference": cell["source_reference"],
                },
                {
                    "cell_id": "c6",
                    "column": 6,
                    "text": "1",
                    "bbox": {
                        "x": 596,
                        "y": 144,
                        "width": 4,
                        "height": 12,
                    },
                },
            ],
        ],
    }

    gold = TableGoldGenerator().generate(table)
    row = gold["rows"][0]

    assert row["chemical_name"]["value_status"] != "merged_cell"
    assert len(row["cas_numbers"]) == 2
    assert all(
        c["source_cell_id"] == "cell_table_046_01_1_5"
        for c in row["cas_numbers"]
    )
    assert row["CAS"]["value"] == "115096-11-2"


@pytest.mark.skipif(
    not PDF_PATH.exists(),
    reason="OHE6.pdf not available",
)
def test_row_anchor_ordering_72_73_separate():
    """
    Physical row anchors 72 and 73 must remain separate.

    Page 54 owns physical row 72 and page 55 starts with physical row 73.
    This is the authoritative regression test for physical row numbering.
    """

    t54 = recover_chemical_oel_table(PDF_PATH, 54)
    t55 = recover_chemical_oel_table(PDF_PATH, 55)

    assert t54 is not None
    assert t55 is not None

    p54_nums = [
        c.text.strip()
        for c in t54.flat_cells()
        if c.column == 6 and c.row > 0
    ]

    p55_nums = [
        c.text.strip()
        for c in t55.flat_cells()
        if c.column == 6 and c.row > 0
    ]

    assert p54_nums, "page 54 must contain physical row anchors"
    assert p55_nums, "page 55 must contain physical row anchors"

    assert p54_nums[-1] == "72"
    assert p55_nums[0] == "73"

    assert p54_nums.count("72") == 1
    assert p55_nums.count("73") == 1


def test_numeric_integrity_rejects_73_when_source_is_72():
    """
    Validator must raise a CRITICAL issue when an extracted row number
    differs from the source evidence.
    """

    evidence_cells = [
        {
            "cell_id": "cell_table_054_01_8_6",
            "text": "72",
            "bbox": {
                "x": 596,
                "y": 370,
                "width": 4,
                "height": 12,
            },
        }
    ]

    validator = GoldsetValidator(evidence_cells)

    issues = validator.validate_table_field(
        "row_number",
        {
            "value": "73",
            "original_value": "72",
            "cell_id": "cell_table_054_01_8_6",
            "bbox": {
                "x": 596,
                "y": 370,
                "width": 4,
                "height": 12,
            },
            "value_status": "extracted",
        },
    )

    assert any(
        "CRITICAL" in issue
        for issue in issues
    )

    assert any(
        "73" in issue and "72" in issue
        for issue in issues
    )


@pytest.mark.skipif(
    not PDF_PATH.exists(),
    reason="OHE6.pdf not available",
)
def test_page_54_source_artifact_contains_row_72():
    """
    The canonical page-54 grid must preserve physical row 72.

    This test deliberately validates the source/canonical artifact rather
    than assuming that TableGoldGenerator is responsible for assigning
    physical PDF row numbers.
    """

    grid_path = (
        PROJECT_ROOT
        / "data"
        / "canonical"
        / "grids"
        / "16518156ffd4_46-55"
        / "table_054_01.json"
    )

    assert grid_path.exists(), (
        f"canonical grid not found: {grid_path}"
    )

    grid = json.loads(
        grid_path.read_text(
            encoding="utf-8"
        )
    )

    rows = [
        grid["headers"],
        *[
            r["cells"]
            for r in grid["rows"]
        ],
    ]

    row_number_cells = [
        cell
        for row in rows
        for cell in row
        if cell.get("column") == 6
    ]

    values = {
        str(
            cell.get("text", "")
        ).strip()
        for cell in row_number_cells
    }

    assert "72" in values, (
        "canonical page-54 grid must preserve "
        "physical row number 72"
    )

    assert "73" not in values, (
        "page-54 canonical grid must not assign "
        "physical row number 73 to the final row"
    )
