"""Tests for pipeline stage contracts."""

from __future__ import annotations

from pipeline_contracts.canonical_grid import CanonicalGridBuilder
from pipeline_contracts.canonical_table import CanonicalTableBuilder
from pipeline_contracts.confidence import LayeredConfidence
from pipeline_contracts.table_family_classifier import TableFamilyClassifier
from pipeline_contracts.validation_codes import ValidationErrorCode, ValidationIssue
from validation_engine.engine import ValidationEngine


def test_layered_confidence_overall_is_minimum():
    conf = LayeredConfidence(layout=0.99, geometry=0.95, mapping=0.92, validation=1.0)
    assert conf.overall == 0.92


def test_validation_issue_typed_codes():
    issue = ValidationIssue(
        code=ValidationErrorCode.MERGED_CELL_UNRESOLVED,
        message="merged cell requires human review",
        field_name="chemical_name",
    )
    payload = issue.to_dict()
    assert payload["code"] == "MERGED_CELL_UNRESOLVED"
    assert payload["field"] == "chemical_name"


def test_canonical_grid_from_structural_table():
    table = {
        "table_id": "table_047_01",
        "page_number": 47,
        "structural_confidence": 0.91,
        "rows": [
            [
                {
                    "cell_id": "cell_table_047_01_0_0",
                    "row": 0,
                    "column": 0,
                    "text": "TWA",
                    "source": "document_ai",
                }
            ],
            [
                {
                    "cell_id": "cell_table_047_01_1_5",
                    "row": 1,
                    "column": 5,
                    "text": "Acetonitrile [75-05-8]",
                    "source": "pymupdf_recovery",
                    "source_reference": {"row_span": 1, "column_span": 1},
                }
            ],
        ],
    }
    grid = CanonicalGridBuilder(
        pipeline_version="1.0.0",
        processor_version="0.1.0",
        evidence_refs=["data/evidence/abc/document_ai_raw.json"],
    ).from_table_dict(table)

    assert grid.table_id == "table_047_01"
    assert "pymupdf_recovery" in grid.source_processors
    assert grid.rows[0].cells[0].text.startswith("Acetonitrile")
    assert grid.confidence.geometry == 0.91


def test_table_family_classifier_chemical_oel():
    table = {
        "table_id": "table_047_01",
        "page_number": 47,
        "structural_confidence": 0.9,
        "rows": [
            [{"cell_id": "h0", "row": 0, "column": 3, "text": "TWA", "source": "document_ai"}],
            [
                {
                    "cell_id": "d0",
                    "row": 1,
                    "column": 5,
                    "text": "Acetonitrile [75-05-8] 20 ppm",
                    "source": "document_ai",
                }
            ],
        ],
    }
    result = TableFamilyClassifier().classify_table_dict(table)
    assert result.schema_id == "chemical_oel_v1"
    assert result.confidence >= 0.85
    assert result.classifier == "deterministic_rules"


def test_canonical_table_from_table_gold():
    from pipeline_contracts.table_family_classifier import TableFamilyClassification

    table_gold = {
        "table_id": "table_047_01",
        "page_number": 47,
        "table_type": "chemical_oel",
        "structural_confidence": 0.9,
        "rows": [
            {
                "chemical_name": {
                    "value": "Acetonitrile",
                    "cell_id": "cell_a",
                    "value_status": "extracted",
                },
                "CAS": {"value": "75-05-8", "cell_id": "cell_b", "value_status": "extracted"},
                "TWA": {"value": "20", "unit": "ppm", "cell_id": "cell_c", "value_status": "extracted"},
            }
        ],
    }
    classification = TableFamilyClassification(
        schema_id="chemical_oel_v1",
        schema_version="1",
        table_type="chemical_oel",
        confidence=0.98,
        classifier="deterministic_rules",
    )
    canonical = CanonicalTableBuilder.from_table_gold(
        table_gold,
        classification,
        pipeline_version="1.0.0",
        processor_version="0.1.0",
    )
    payload = canonical.to_dict()
    assert payload["schema"] == "chemical_oel_v1"
    assert "chemical_name" in payload["headers"]
    assert payload["rows"][0]["fields"]["CAS"]["value"] == "75-05-8"


def test_validation_engine_merged_cell_code():
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
    assert result.issues[0].code == ValidationErrorCode.MERGED_CELL_UNRESOLVED
