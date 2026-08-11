"""Comprehensive tests for HITL review workflow."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from database.models import (
    CandidateStatus,
    ReviewDecisionType,
    ReviewPriority,
    ReviewTaskStatus,
    ReviewTargetType,
)
from review.acceptance_contract import contract_passed, evaluate_acceptance_contract
from review.review_priority import compute_priority, priority_rank
from review.candidate_store import CandidateStore
from goldset_generator.validator import GoldsetValidator
from goldset_generator.table_gold_generator import _parse_exposure_value


class TestAcceptanceContract:
    def test_all_passed(self):
        result = evaluate_acceptance_contract(
            {k: "passed" for k in [
                "numeric_integrity", "cell_reference_integrity", "bbox_integrity",
                "table_structure", "cas_validation", "unit_validation", "semantic_validation",
            ]}
        )
        assert result["passed"] is True

    def test_numeric_failure_blocks(self):
        result = evaluate_acceptance_contract(
            {
                "numeric_integrity": "review_required",
                "cell_reference_integrity": "passed",
                "bbox_integrity": "passed",
                "table_structure": "passed",
                "cas_validation": "passed",
                "unit_validation": "passed",
                "semantic_validation": "passed",
            }
        )
        assert result["passed"] is False

    def test_high_confidence_does_not_override_failure(self):
        contract = evaluate_acceptance_contract({"numeric_integrity": "failed", "cell_reference_integrity": "passed",
            "bbox_integrity": "passed", "table_structure": "passed", "cas_validation": "passed",
            "unit_validation": "passed", "semantic_validation": "passed"})
        assert contract_passed(contract["contract"]) is False


class TestPriority:
    def test_numeric_is_critical(self):
        assert compute_priority(["NUMERIC_NORMALIZATION"]) == ReviewPriority.CRITICAL

    def test_ambiguous_is_medium(self):
        assert compute_priority(["ambiguous_header_mapping"]) == ReviewPriority.MEDIUM

    def test_rank_order(self):
        assert priority_rank(ReviewPriority.CRITICAL) < priority_rank(ReviewPriority.LOW)


class TestCandidateStore:
    def test_apply_header_correction(self):
        session = MagicMock()
        store = CandidateStore(session)
        payload = {
            "header_mapping": {"1": "symbols", "3": "TWA", "4": "TWA"},
            "rows": [],
        }
        updated = store.apply_corrections_to_payload(payload, [
            {"field_name": "header_mapping.4", "corrected_value": "STEL", "correction_type": "header_mapping"},
        ])
        assert updated["header_mapping"]["4"] == "STEL"
        assert payload["header_mapping"]["4"] == "TWA"

    def test_apply_table_cell_correction_without_overwriting_evidence(self):
        session = MagicMock()
        store = CandidateStore(session)
        payload = {
            "rows": [{
                "TWA": {
                    "value": "3",
                    "original_value": "۳ mg/m³",
                    "normalized_value": "3 mg/m³",
                }
            }]
        }
        updated = store.apply_corrections_to_payload(payload, [
            {
                "field_name": "rows.0.TWA.value",
                "corrected_value": "2",
                "correction_type": "field_value",
            },
        ])
        assert updated["rows"][0]["TWA"]["value"] == "2"
        assert updated["rows"][0]["TWA"]["original_value"] == "۳ mg/m³"
        assert payload["rows"][0]["TWA"]["value"] == "3"

    def test_remove_phantom_header_mapping(self):
        session = MagicMock()
        store = CandidateStore(session)
        payload = {
            "header_mapping": {"5": "row_number", "6": "row_number"},
            "rows": [],
        }
        updated = store.apply_corrections_to_payload(payload, [
            {
                "field_name": "header_mapping.6",
                "corrected_value": None,
                "correction_type": "structure",
            },
        ])
        assert updated["header_mapping"] == {"5": "row_number"}
        assert payload["header_mapping"]["6"] == "row_number"

    def test_materialize_missing_stel_column_from_immutable_evidence(self):
        payload = {
            "rows": [{
                "TWA": {
                    "value": "1",
                    "cell_id": "cell_table_050_01_2_3",
                    "original_value": "1 ppm",
                },
                "chemical_name": {
                    "value": "2-Aminobutanol",
                    "cell_id": "cell_table_050_01_2_5",
                },
            }]
        }
        evidence_cells = [{
            "cell_id": "cell_table_050_01_2_2",
            "row": 2,
            "column": 2,
            "text": "۲ ppm",
            "normalized_value": "2 ppm",
            "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
            "source_reference": {
                "page_number": 50,
                "cell_ids": ["cell_table_050_01_2_2"],
            },
        }]

        CandidateStore.materialize_table_column(
            payload,
            table_id="table_050_01",
            source_column=2,
            target_field="STEL",
            evidence_cells=evidence_cells,
        )

        assert payload["rows"][0]["STEL"]["value"] == "۲"
        assert payload["rows"][0]["STEL"]["original_value"] == "۲ ppm"
        assert payload["rows"][0]["STEL"]["cell_id"] == "cell_table_050_01_2_2"
        assert payload["rows"][0]["TWA"]["value"] == "1"

    def test_normalize_stel_c_header_alias(self):
        assert CandidateStore.normalize_header_field("STEL/C") == "STEL"


class TestNumericTraceability:
    @pytest.mark.parametrize(
        ("evidence", "candidate"),
        [
            ("۲۶۷/۳۷", "267.37"),
            ("۳۱۷/۳۴", "317.34"),
            ("١١٦/٠٨", "116.08"),
            ("۱۳۷/۳۰", "137.30"),
            ("۲۳۳/۴۳", "233.43"),
            ("٢٢٣/٢٠", "٢٢٣.٢٠"),
            ("٢٩٠/٣٢", "٢٩٠.٣٢"),
        ],
    )
    def test_persian_slash_decimal_matches_ascii_decimal(self, evidence, candidate):
        validator = GoldsetValidator([
            {
                "cell_id": "cell_1",
                "text": evidence,
                "normalized_value": evidence,
            }
        ])
        assert validator.numeric_exists_in_cell("cell_1", candidate)

    def test_unit_exponent_is_not_extracted_as_exposure_value(self):
        value, unit = _parse_exposure_value(r"\cdot/\Delta mg/m^{3}")
        assert value is None
        assert unit == "mg/m³"

    def test_real_value_before_latex_unit_is_preserved(self):
        value, unit = _parse_exposure_value(r"4~mg/m^{3(l)}")
        assert value == "4"
        assert unit == "mg/m³"


class TestReviewAssignment:
    def test_concurrent_claim_blocked(self):
        from review.review_assignment import ClaimError, ReviewAssignment
        from database.models import ReviewTask, ReviewTaskStatus

        session = MagicMock()
        task = ReviewTask(
            id=uuid.uuid4(),
            status=ReviewTaskStatus.IN_PROGRESS,
            assigned_to="reviewer_a",
        )
        session.get.return_value = task
        assignment = ReviewAssignment(session)
        assignment.expire_stale_claims = MagicMock()
        with pytest.raises(ClaimError, match="already claimed"):
            assignment.claim(task.id, "reviewer_b")


class TestHumanOnlyApproval:
    def test_llm_cannot_approve(self):
        from review.review_service import ReviewService, ReviewServiceError

        service = ReviewService(MagicMock())
        with pytest.raises(ReviewServiceError, match="human"):
            service._ensure_human_reviewer("llm:gemini")


class TestIdempotencyKey:
    def test_grouping_key_format(self):
        from review.task_factory import _idempotency_key

        doc_id = uuid.uuid4()
        key = _idempotency_key(doc_id, "1.0.0", "table", "table_047_01")
        assert "table_047_01" in key
        assert str(doc_id) in key
