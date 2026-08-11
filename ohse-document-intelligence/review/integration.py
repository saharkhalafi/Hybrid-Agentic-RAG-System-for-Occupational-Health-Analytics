"""Pipeline integration — create HITL tasks after goldset validation."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from review.candidate_store import CandidateStore
from review.revalidation import validate_table_candidate
from review.task_factory import create_cases_from_page_validation


def sync_page_to_hitl(
    session: Session,
    *,
    document_id: uuid.UUID,
    page_number: int,
    validation_report: dict[str, Any],
    table_golds: list[dict[str, Any]],
    pipeline_version: str,
) -> list:
    """Called from goldset pipeline after validation — creates candidates, runs, and review cases."""
    store = CandidateStore(session)
    candidates_by_table = {}

    for table_gold in table_golds:
        tid = table_gold.get("table_id")
        if not tid:
            continue
        candidate = store.create_from_payload(
            document_id=document_id,
            candidate_type="table",
            stable_id=tid,
            payload=table_gold,
            page_number=page_number,
        )
        candidates_by_table[tid] = candidate

    validation_run = None
    if validation_report.get("overall_status") in {"review_required", "failed"} and candidates_by_table:
        first = next(iter(candidates_by_table.values()))
        validation_run = validate_table_candidate(session, first, pipeline_version=pipeline_version)

    return create_cases_from_page_validation(
        session,
        document_id=document_id,
        page_number=page_number,
        validation_report=validation_report,
        candidates_by_table=candidates_by_table,
        validation_run=validation_run,
        pipeline_version=pipeline_version,
    )
