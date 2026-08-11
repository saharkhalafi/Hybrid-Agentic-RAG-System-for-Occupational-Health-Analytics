"""Create human review cases from machine validation — grouped by target, idempotent."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import (
    ExtractionCandidate,
    ReviewEventType,
    ReviewTargetType,
    ReviewTask,
    ReviewTaskStatus,
    ReviewerType,
    ValidationRun,
)
from review.issue_explainer import dedupe_issues, explain_issue, human_issue_title
from review.review_events import log_event
from review.review_priority import compute_priority


def _idempotency_key(
    document_id: uuid.UUID,
    pipeline_version: str,
    target_type: str,
    target_id: str,
) -> str:
    return f"{document_id}:{pipeline_version}:{target_type}:{target_id}"


def _issue_to_code(issue: dict[str, Any]) -> str:
    raw = issue.get("code") or issue.get("type") or "UNKNOWN"
    return str(raw).upper().replace("-", "_")


def _build_task_title(target_type: ReviewTargetType, target_id: str, primary_code: str) -> str:
    return f"{target_type.value.replace('_', ' ').title()} {target_id} — {human_issue_title(primary_code)}"


def _build_description(issues: list[dict[str, Any]]) -> str:
    """Render a plain-language, actionable explanation for each issue in the case."""
    return "\n\n".join(explain_issue(issue) for issue in issues)


def upsert_review_case(
    session: Session,
    *,
    document_id: uuid.UUID,
    page_number: int | None,
    target_type: ReviewTargetType,
    target_id: str,
    issues: list[dict[str, Any]],
    candidate_id: uuid.UUID | None = None,
    validation_run_id: uuid.UUID | None = None,
    stable_table_id: str | None = None,
    evidence_reference: dict[str, Any] | None = None,
    evidence_snapshot: dict[str, Any] | None = None,
    pipeline_version: str | None = None,
) -> ReviewTask | None:
    """Create or update a grouped human review case. Returns None if no issues."""
    issues = dedupe_issues(issues)
    if not issues:
        return None

    settings = get_settings()
    pv = pipeline_version or settings.goldset_pipeline_version
    idem = _idempotency_key(document_id, pv, target_type.value, target_id)

    existing = session.scalar(
        select(ReviewTask).where(
            ReviewTask.idempotency_key == idem,
            ReviewTask.status.in_([ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS]),
        )
    )
    if existing is None:
        # A resolved/cancelled/rejected task may already occupy this idempotency_key
        # from an earlier pipeline run (append-only queues can resurface old issues).
        # Reopen it instead of inserting a duplicate, which would violate the
        # unique constraint on idempotency_key.
        existing = session.scalar(select(ReviewTask).where(ReviewTask.idempotency_key == idem))
        if existing is not None:
            existing.status = ReviewTaskStatus.PENDING
            existing.resolved_at = None
            existing.assigned_to = None
            existing.claimed_at = None
            existing.claim_expires_at = None

    codes = [_issue_to_code(i) for i in issues]
    severities = [str(i.get("severity", "medium")).lower() for i in issues]
    priority = compute_priority(codes, severities=severities)
    primary_code = codes[0]
    title = _build_task_title(target_type, target_id, primary_code)
    description = _build_description(issues)
    enriched_issues = [{**issue, "human_message": explain_issue(issue)} for issue in issues]

    if existing:
        existing.issue_metadata = {"issues": enriched_issues, "issue_codes": codes}
        existing.primary_issue_code = primary_code
        existing.severity = severities[0]
        existing.priority = priority
        existing.description = description
        existing.validation_run_id = validation_run_id or existing.validation_run_id
        existing.candidate_id = candidate_id or existing.candidate_id
        existing.version += 1
        return existing

    task = ReviewTask(
        document_id=document_id,
        page_number=page_number,
        stable_table_id=stable_table_id or (target_id if target_type == ReviewTargetType.TABLE else None),
        candidate_id=candidate_id,
        validation_run_id=validation_run_id,
        target_type=target_type,
        target_id=target_id,
        primary_issue_code=primary_code,
        severity=severities[0],
        title=title,
        description=description,
        status=ReviewTaskStatus.PENDING,
        priority=priority,
        pipeline_version=pv,
        idempotency_key=idem,
        evidence_reference=evidence_reference,
        evidence_snapshot=evidence_snapshot,
        issue_metadata={"issues": enriched_issues, "issue_codes": codes},
    )
    session.add(task)
    session.flush()

    log_event(
        session,
        review_task_id=task.id,
        event_type=ReviewEventType.TASK_CREATED,
        actor_id="system",
        actor_type=ReviewerType.SYSTEM,
        metadata={"issue_count": len(issues), "target_id": target_id},
    )
    return task


def create_cases_from_page_validation(
    session: Session,
    *,
    document_id: uuid.UUID,
    page_number: int,
    validation_report: dict[str, Any],
    candidates_by_table: dict[str, ExtractionCandidate] | None = None,
    validation_run: ValidationRun | None = None,
    pipeline_version: str | None = None,
) -> list[ReviewTask]:
    """Group page validation into human review cases (one per table, one per page if needed)."""
    tasks: list[ReviewTask] = []
    candidates_by_table = candidates_by_table or {}

    if validation_report.get("overall_status") not in {"review_required", "failed"}:
        return tasks

    table_issues: dict[str, list[dict[str, Any]]] = {}
    page_level_issues: list[dict[str, Any]] = []

    for issue in validation_report.get("issues") or []:
        issue_type = str(issue.get("type", ""))
        if issue_type in {"entity", "qa"}:
            page_level_issues.append(issue)
        else:
            for table in validation_report.get("tables") or []:
                tid = table.get("table_id", f"page_{page_number}")
                table_issues.setdefault(tid, []).extend(table.get("mapping_issues") or [])
                if issue_type in {"ambiguous_header_mapping", "incomplete_header_mapping"}:
                    table_issues.setdefault(tid, []).append(issue)

    for table in validation_report.get("tables") or []:
        tid = table.get("table_id")
        if not tid:
            continue
        issues = list(table_issues.get(tid, []))
        for issue in validation_report.get("issues") or []:
            if issue.get("type") == "entity":
                issues.append(issue)

        candidate = candidates_by_table.get(tid)
        task = upsert_review_case(
            session,
            document_id=document_id,
            page_number=page_number,
            target_type=ReviewTargetType.TABLE,
            target_id=tid,
            issues=issues,
            candidate_id=candidate.id if candidate else None,
            validation_run_id=validation_run.id if validation_run else None,
            stable_table_id=tid,
            evidence_snapshot={"table_report": table},
            pipeline_version=pipeline_version,
        )
        if task:
            tasks.append(task)

    if page_level_issues and not tasks:
        task = upsert_review_case(
            session,
            document_id=document_id,
            page_number=page_number,
            target_type=ReviewTargetType.PAGE,
            target_id=f"page_{page_number:03d}",
            issues=page_level_issues,
            validation_run_id=validation_run.id if validation_run else None,
            pipeline_version=pipeline_version,
        )
        if task:
            tasks.append(task)

    return tasks
