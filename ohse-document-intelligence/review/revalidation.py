"""Revalidation orchestrator — runs after APPROVE or CORRECT, never bypasses acceptance contract."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from database.models import CandidateStatus, ExtractionCandidate, ValidationIssueRecord, ValidationRun
from goldset_generator.validation_report import build_page_validation_report, build_table_validation_report
from goldset_generator.validator import GoldsetValidator
from goldset_generator.header_mapping import analyze_header_mapping
from pipeline_contracts.validation_codes import ValidationErrorCode, ValidationIssue
from review.acceptance_contract import evaluate_acceptance_contract
from review.candidate_store import CandidateStore


def _normalize_issue_code(raw: str) -> str:
    mapping = {
        "ambiguous_header_mapping": ValidationErrorCode.COLUMN_AMBIGUOUS.value,
        "entity": ValidationErrorCode.VALUE_NOT_IN_EVIDENCE.value,
        "incomplete_header_mapping": ValidationErrorCode.HEADER_UNKNOWN.value,
    }
    return mapping.get(raw, raw.upper().replace("-", "_"))


def _load_evidence_cells(candidate: ExtractionCandidate) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    payload = candidate.payload
    for row in payload.get("rows") or []:
        for field_data in row.values():
            if isinstance(field_data, dict) and field_data.get("cell_id"):
                cells.append(
                    {
                        "cell_id": field_data["cell_id"],
                        "text": field_data.get("original_value") or field_data.get("value"),
                        "normalized_value": field_data.get("normalized_value"),
                        "bbox": field_data.get("bbox"),
                    }
                )
    return cells


def validate_table_candidate(
    session: Session,
    candidate: ExtractionCandidate,
    *,
    pipeline_version: str,
) -> ValidationRun:
    """Run full validation on a table candidate and persist validation_run + machine issues."""
    store = CandidateStore(session)
    run_number = store.next_run_number(candidate.id)
    payload = candidate.payload
    page_number = candidate.page_number or payload.get("page_number", 0)

    if payload.get("header_mapping"):
        int_mapping = {int(k): v for k, v in payload["header_mapping"].items() if str(k).isdigit()}
        if int_mapping:
            analyzed = analyze_header_mapping(int_mapping)
            payload = {**payload, **analyzed}

    evidence_cells = _load_evidence_cells(candidate)
    validator = GoldsetValidator(evidence_cells)

    entity_issues: list[dict[str, Any]] = []
    numeric_failures = 0
    row_review_count = 0

    for row in payload.get("rows") or []:
        for fname, fval in row.items():
            if not isinstance(fval, dict):
                continue
            field_issues = validator.validate_table_field(fname, fval)
            if field_issues:
                numeric_failures += len(field_issues)
                entity_issues.append({"issues": field_issues})
            if fval.get("value_status") in {"extraction_uncertain", "merged_cell"}:
                row_review_count += 1

    mapping_meta = {
        "mapping_confidence": payload.get("mapping_confidence"),
        "mapping_status": payload.get("mapping_status", "accepted"),
        "mapping_issues": payload.get("mapping_issues") or [],
    }
    table_report = build_table_validation_report(
        table_id=candidate.stable_id,
        page_number=page_number,
        mapping_meta=mapping_meta,
        row_review_count=row_review_count,
    )
    page_report = build_page_validation_report(
        page_number=page_number,
        table_reports=[table_report],
        entity_issues=entity_issues,
        qa_issues=[],
        numeric_failures=numeric_failures,
    )

    contract_result = evaluate_acceptance_contract(page_report["validation"])
    passed = contract_result["passed"] and page_report["overall_status"] == "passed"

    run = ValidationRun(
        document_id=candidate.document_id,
        candidate_id=candidate.id,
        target_type="table",
        target_id=candidate.stable_id,
        run_number=run_number,
        validation_report=page_report,
        acceptance_contract=contract_result["contract"],
        overall_status=contract_result["overall_status"],
        passed=passed,
        pipeline_version=pipeline_version,
    )
    session.add(run)
    session.flush()

    machine_issues: list[ValidationIssueRecord] = []
    for issue in page_report.get("issues") or []:
        code = _normalize_issue_code(str(issue.get("type", "unknown")))
        record = ValidationIssueRecord(
            validation_run_id=run.id,
            issue_code=code,
            severity=issue.get("severity", "medium"),
            message=issue.get("message", ""),
            target_type="table",
            target_id=candidate.stable_id,
            metadata_=issue,
        )
        session.add(record)
        machine_issues.append(record)

    for issue in payload.get("mapping_issues") or []:
        code = _normalize_issue_code(str(issue.get("type", "unknown")))
        record = ValidationIssueRecord(
            validation_run_id=run.id,
            issue_code=code,
            severity=issue.get("severity", "medium"),
            message=issue.get("message", ""),
            field_name=str(issue.get("fields", "")),
            target_type="table",
            target_id=candidate.stable_id,
            metadata_=issue,
        )
        session.add(record)
        machine_issues.append(record)

    session.flush()
    return run


def validate_formula_candidate(
    session: Session,
    candidate: ExtractionCandidate,
    *,
    pipeline_version: str,
) -> ValidationRun:
    """Run FormulaValidator on a formula candidate and persist validation_run + issues.

    Formula gold payloads already embed their own evidence fragments (candidate_text,
    raw_text_fragments, nearby text), so no separate page_text lookup is required.
    """
    from goldset_generator.formula_validator import FormulaValidator

    store = CandidateStore(session)
    run_number = store.next_run_number(candidate.id)
    payload = candidate.payload
    reconstruction_status = (
        (payload.get("reconstruction") or {}).get("status") or payload.get("status") or "extraction_uncertain"
    )

    validator = FormulaValidator()
    result = validator.validate(payload, "", reconstruction_status=reconstruction_status)
    passed = result["overall"] == "approved"

    run = ValidationRun(
        document_id=candidate.document_id,
        candidate_id=candidate.id,
        target_type="formula",
        target_id=candidate.stable_id,
        run_number=run_number,
        validation_report=result,
        acceptance_contract={
            "structural": result["structural"],
            "symbol_traceability": result["symbol_traceability"],
            "numeric_traceability": result["numeric_traceability"],
            "unit_consistency": result["unit_consistency"],
        },
        overall_status=result["overall"],
        passed=passed,
        pipeline_version=pipeline_version,
    )
    session.add(run)
    session.flush()

    for issue in result.get("issues") or []:
        record = ValidationIssueRecord(
            validation_run_id=run.id,
            issue_code=str(issue.get("code", "UNKNOWN")),
            severity=issue.get("severity", "error"),
            message=issue.get("message", ""),
            field_name=issue.get("field"),
            target_type="formula",
            target_id=candidate.stable_id,
            metadata_=issue.get("metadata") or {},
        )
        session.add(record)

    session.flush()
    return run


def validate_candidate(
    session: Session,
    candidate: ExtractionCandidate,
    *,
    pipeline_version: str,
) -> ValidationRun:
    """Dispatch to the correct validator based on candidate type."""
    if candidate.candidate_type == "formula":
        return validate_formula_candidate(session, candidate, pipeline_version=pipeline_version)
    return validate_table_candidate(session, candidate, pipeline_version=pipeline_version)


def apply_validation_outcome(
    session: Session,
    candidate: ExtractionCandidate,
    run: ValidationRun,
) -> str:
    """Update candidate status based on validation outcome. Returns outcome label."""
    store = CandidateStore(session)
    if run.passed:
        store.mark_accepted(candidate)
        return "accepted"
    return "review_required"
