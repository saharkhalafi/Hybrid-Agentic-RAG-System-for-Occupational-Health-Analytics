"""Main HITL review service — orchestrates claim, decide, revalidate workflow."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import (
    CorrectionType,
    ExtractionCandidate,
    ReviewCorrection,
    ReviewDecision,
    ReviewDecisionType,
    ReviewEvent,
    ReviewTask,
    ReviewTaskStatus,
    ReviewerType,
    ValidationRun,
)
from review.candidate_store import CandidateStore
from review.evidence_store import flatten_table_evidence_cells
from review.issue_explainer import explain_issue
from review.review_assignment import ClaimError, ReviewAssignment
from review.review_events import log_event
from review.review_priority import priority_rank
from review.revalidation import apply_validation_outcome, validate_candidate
from database.models import ReviewEventType, ReviewPriority


class ReviewServiceError(Exception):
    pass


class ReviewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.assignment = ReviewAssignment(session)
        self.candidates = CandidateStore(session)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        issue_code: str | None = None,
        document_id: uuid.UUID | None = None,
        page_number: int | None = None,
        target_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ReviewTask], int]:
        self.assignment.expire_stale_claims()
        query = select(ReviewTask)
        count_query = select(func.count()).select_from(ReviewTask)

        filters = []
        if status:
            filters.append(ReviewTask.status == status)
        if priority:
            filters.append(ReviewTask.priority == priority)
        if issue_code:
            filters.append(ReviewTask.primary_issue_code == issue_code)
        if document_id:
            filters.append(ReviewTask.document_id == document_id)
        if page_number is not None:
            filters.append(ReviewTask.page_number == page_number)
        if target_type:
            filters.append(ReviewTask.target_type == target_type)

        for f in filters:
            query = query.where(f)
            count_query = count_query.where(f)

        total = self.session.scalar(count_query) or 0
        tasks = list(
            self.session.scalars(
                query.order_by(ReviewTask.priority, ReviewTask.created_at).offset(offset).limit(limit)
            ).all()
        )
        tasks.sort(key=lambda t: (priority_rank(t.priority), t.created_at or datetime.min.replace(tzinfo=timezone.utc)))
        return tasks, total

    def get_task(self, task_id: uuid.UUID) -> ReviewTask | None:
        self.assignment.expire_stale_claims()
        return self.session.get(ReviewTask, task_id)

    def get_task_detail(self, task_id: uuid.UUID) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if not task:
            return None

        candidate_payload = None
        if task.candidate_id:
            candidate = self.candidates.get_by_id(task.candidate_id)
            if candidate:
                candidate_payload = candidate.payload

        validation_report = None
        machine_issues: list[dict[str, Any]] = []
        if task.validation_run_id:
            run = self.session.get(ValidationRun, task.validation_run_id)
            if run:
                validation_report = run.validation_report
                machine_issues = [
                    {
                        "issue_code": i.issue_code,
                        "severity": i.severity,
                        "message": i.message,
                        "field_name": i.field_name,
                        "human_message": explain_issue(
                            {
                                "code": i.issue_code,
                                "severity": i.severity,
                                "message": i.message,
                                "field": i.field_name,
                                "metadata": i.metadata_ or {},
                            }
                        ),
                    }
                    for i in run.machine_issues
                ]

        return {
            "task": task,
            "candidate_payload": candidate_payload,
            "validation_report": validation_report,
            "machine_issues": machine_issues,
        }

    def claim_task(self, task_id: uuid.UUID, reviewer_id: str) -> ReviewTask:
        return self.assignment.claim(task_id, reviewer_id)

    def release_task(self, task_id: uuid.UUID, reviewer_id: str) -> ReviewTask:
        return self.assignment.release(task_id, reviewer_id)

    def _ensure_human_reviewer(self, reviewer_id: str) -> None:
        if not reviewer_id or reviewer_id.startswith("llm:"):
            raise ReviewServiceError("Only human reviewers may submit approval decisions")

    def _get_candidate_for_task(self, task: ReviewTask) -> ExtractionCandidate:
        if not task.candidate_id:
            raise ReviewServiceError(
                "This task has no linked candidate (observational issue, e.g. a text-chunk "
                "or unlinked entity note) — there is nothing to edit. Use APPROVE to resolve "
                "it as confirmed, or REJECT with a reason."
            )
        candidate = self.candidates.get_by_id(task.candidate_id)
        if not candidate:
            raise ReviewServiceError("Candidate not found")
        return candidate

    def approve_task(
        self,
        task_id: uuid.UUID,
        reviewer_id: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_human_reviewer(reviewer_id)
        task = self.session.get(ReviewTask, task_id, with_for_update=True)
        if not task:
            raise ReviewServiceError("Task not found")
        if task.status != ReviewTaskStatus.IN_PROGRESS or task.assigned_to != reviewer_id:
            raise ReviewServiceError("Task must be claimed by this reviewer")

        prev_status = task.status.value

        if not task.candidate_id:
            # Observational review item (e.g. legacy entity/text-chunk issue) with no
            # versioned candidate behind it — nothing to revalidate. APPROVE simply
            # records the human confirmation and resolves the case.
            decision = ReviewDecision(
                review_task_id=task.id,
                reviewer_id=reviewer_id,
                reviewer_type=ReviewerType.HUMAN,
                decision=ReviewDecisionType.APPROVE,
                comment=comment,
                previous_status=prev_status,
                new_status=ReviewTaskStatus.RESOLVED.value,
            )
            self.session.add(decision)
            task.status = ReviewTaskStatus.RESOLVED
            task.resolved_at = datetime.now(timezone.utc)

            log_event(
                self.session,
                review_task_id=task.id,
                event_type=ReviewEventType.DECISION_SUBMITTED,
                actor_id=reviewer_id,
                metadata={"decision": "approve", "candidate_linked": False},
            )
            log_event(
                self.session, review_task_id=task.id, event_type=ReviewEventType.TASK_RESOLVED, actor_id=reviewer_id
            )
            return {
                "task_id": task.id,
                "decision": "approve",
                "new_status": task.status.value,
                "validation_run_id": None,
                "candidate_id": None,
                "candidate_status": None,
                "passed": True,
            }

        candidate = self._get_candidate_for_task(task)

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.VALIDATION_STARTED,
            actor_id=reviewer_id,
            metadata={"decision": "approve"},
        )

        run = validate_candidate(
            self.session,
            candidate,
            pipeline_version=task.pipeline_version,
        )
        outcome = apply_validation_outcome(self.session, candidate, run)

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.VALIDATION_COMPLETED,
            actor_id="system",
            actor_type=ReviewerType.SYSTEM,
            metadata={"passed": run.passed, "outcome": outcome},
        )

        decision = ReviewDecision(
            review_task_id=task.id,
            reviewer_id=reviewer_id,
            reviewer_type=ReviewerType.HUMAN,
            decision=ReviewDecisionType.APPROVE,
            comment=comment,
            previous_status=prev_status,
            new_status=ReviewTaskStatus.RESOLVED.value if run.passed else ReviewTaskStatus.PENDING.value,
            validation_run_id=run.id,
        )
        self.session.add(decision)
        self.session.flush()

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.DECISION_SUBMITTED,
            actor_id=reviewer_id,
            metadata={"decision": "approve"},
        )

        if run.passed:
            task.status = ReviewTaskStatus.RESOLVED
            task.resolved_at = datetime.now(timezone.utc)
            log_event(self.session, review_task_id=task.id, event_type=ReviewEventType.TASK_RESOLVED, actor_id=reviewer_id)
        else:
            task.status = ReviewTaskStatus.PENDING
            task.assigned_to = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.validation_run_id = run.id

        return {
            "task_id": task.id,
            "decision": "approve",
            "new_status": task.status.value,
            "validation_run_id": run.id,
            "candidate_id": candidate.id,
            "candidate_status": candidate.status.value,
            "passed": run.passed,
        }

    def correct_task(
        self,
        task_id: uuid.UUID,
        reviewer_id: str,
        corrections: list[dict[str, Any]],
        comment: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_human_reviewer(reviewer_id)
        if not corrections:
            raise ReviewServiceError("CORRECT requires at least one correction")

        task = self.session.get(ReviewTask, task_id, with_for_update=True)
        if not task:
            raise ReviewServiceError("Task not found")
        if task.status != ReviewTaskStatus.IN_PROGRESS or task.assigned_to != reviewer_id:
            raise ReviewServiceError("Task must be claimed by this reviewer")

        parent = self._get_candidate_for_task(task)
        prev_status = task.status.value

        decision = ReviewDecision(
            review_task_id=task.id,
            reviewer_id=reviewer_id,
            reviewer_type=ReviewerType.HUMAN,
            decision=ReviewDecisionType.CORRECT,
            comment=comment,
            previous_status=prev_status,
            new_status=ReviewTaskStatus.PENDING.value,
        )
        self.session.add(decision)
        self.session.flush()

        corr_records: list[ReviewCorrection] = []
        for corr in corrections:
            ctype_str = corr.get("correction_type", "field_value")
            try:
                ctype = CorrectionType(ctype_str)
            except ValueError:
                ctype = CorrectionType.OTHER
            corrected_value = corr.get("corrected_value")
            if ctype == CorrectionType.HEADER_MAPPING:
                corrected_value = self.candidates.normalize_header_field(corrected_value)

            record = ReviewCorrection(
                review_decision_id=decision.id,
                review_task_id=task.id,
                target_type=corr.get("target_type") or task.target_type.value,
                target_id=corr.get("target_id") or task.target_id,
                field_name=corr["field_name"],
                original_value=corr.get("original_value"),
                corrected_value=corrected_value,
                correction_type=ctype,
                reason=corr.get("reason"),
            )
            self.session.add(record)
            corr_records.append(record)

            log_event(
                self.session,
                review_task_id=task.id,
                event_type=ReviewEventType.CORRECTION_CREATED,
                actor_id=reviewer_id,
                metadata={"field": corr["field_name"]},
            )

        self.session.flush()

        corrected_payload = self.candidates.apply_corrections_to_payload(
            parent.payload,
            [
                {
                    "field_name": c.field_name,
                    "corrected_value": c.corrected_value,
                    "correction_type": c.correction_type.value,
                }
                for c in corr_records
            ],
        )
        remapped_exposure_columns = [
            correction
            for correction in corr_records
            if correction.correction_type == CorrectionType.HEADER_MAPPING
            and correction.field_name.startswith("header_mapping.")
            and correction.corrected_value in {"TWA", "STEL", "ceiling"}
            and correction.corrected_value != correction.original_value
        ]
        if remapped_exposure_columns:
            evidence_cells = flatten_table_evidence_cells(parent.stable_id)
            if not evidence_cells:
                raise ReviewServiceError(
                    "Cannot restore the remapped column because immutable table evidence was not found"
                )
            exposure_mapping = {
                int(column): field
                for column, field in (corrected_payload.get("header_mapping") or {}).items()
                if str(column).isdigit() and field in {"TWA", "STEL", "ceiling"}
            }
            for source_column, target_field in exposure_mapping.items():
                self.candidates.materialize_table_column(
                    corrected_payload,
                    table_id=parent.stable_id,
                    source_column=source_column,
                    target_field=target_field,
                    evidence_cells=evidence_cells,
                )

            # Structural restoration must happen before reviewer-entered cell
            # corrections, otherwise rebuilding TWA/STEL would overwrite the
            # human's corrected value in the same decision.
            field_corrections = [
                {
                    "field_name": correction.field_name,
                    "corrected_value": correction.corrected_value,
                    "correction_type": correction.correction_type.value,
                }
                for correction in corr_records
                if correction.correction_type == CorrectionType.FIELD_VALUE
            ]
            if field_corrections:
                corrected_payload = self.candidates.apply_corrections_to_payload(
                    corrected_payload,
                    field_corrections,
                )
        new_candidate = self.candidates.create_corrected_version(
            parent,
            corrected_payload=corrected_payload,
            decision_id=decision.id,
        )
        task.candidate_id = new_candidate.id

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.VALIDATION_STARTED,
            actor_id=reviewer_id,
            metadata={"decision": "correct", "new_candidate_version": new_candidate.version},
        )

        run = validate_candidate(
            self.session,
            new_candidate,
            pipeline_version=task.pipeline_version,
        )
        outcome = apply_validation_outcome(self.session, new_candidate, run)
        decision.validation_run_id = run.id
        decision.new_status = (
            ReviewTaskStatus.RESOLVED.value if run.passed else ReviewTaskStatus.PENDING.value
        )

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.VALIDATION_COMPLETED,
            actor_id="system",
            actor_type=ReviewerType.SYSTEM,
            metadata={"passed": run.passed, "outcome": outcome},
        )

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.DECISION_SUBMITTED,
            actor_id=reviewer_id,
            metadata={"decision": "correct", "correction_count": len(corr_records)},
        )

        if run.passed:
            task.status = ReviewTaskStatus.RESOLVED
            task.resolved_at = datetime.now(timezone.utc)
            task.validation_run_id = run.id
            log_event(self.session, review_task_id=task.id, event_type=ReviewEventType.TASK_RESOLVED, actor_id=reviewer_id)
        else:
            task.status = ReviewTaskStatus.PENDING
            task.assigned_to = None
            task.claimed_at = None
            task.claim_expires_at = None
            task.validation_run_id = run.id

        return {
            "task_id": task.id,
            "decision": "correct",
            "new_status": task.status.value,
            "validation_run_id": run.id,
            "candidate_id": new_candidate.id,
            "candidate_status": new_candidate.status.value,
            "passed": run.passed,
        }

    def reject_task(
        self,
        task_id: uuid.UUID,
        reviewer_id: str,
        reason: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_human_reviewer(reviewer_id)
        task = self.session.get(ReviewTask, task_id, with_for_update=True)
        if not task:
            raise ReviewServiceError("Task not found")
        if task.status != ReviewTaskStatus.IN_PROGRESS or task.assigned_to != reviewer_id:
            raise ReviewServiceError("Task must be claimed by this reviewer")

        prev_status = task.status.value
        if task.candidate_id:
            candidate = self.candidates.get_by_id(task.candidate_id)
            if candidate:
                self.candidates.mark_quarantined(candidate)

        decision = ReviewDecision(
            review_task_id=task.id,
            reviewer_id=reviewer_id,
            reviewer_type=ReviewerType.HUMAN,
            decision=ReviewDecisionType.REJECT,
            decision_reason=reason,
            comment=comment,
            previous_status=prev_status,
            new_status=ReviewTaskStatus.REJECTED.value,
        )
        self.session.add(decision)

        task.status = ReviewTaskStatus.REJECTED
        task.resolved_at = datetime.now(timezone.utc)

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.DECISION_SUBMITTED,
            actor_id=reviewer_id,
            metadata={"decision": "reject", "reason": reason},
        )
        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.TASK_REJECTED,
            actor_id=reviewer_id,
            metadata={"reason": reason},
        )

        return {
            "task_id": task.id,
            "decision": "reject",
            "new_status": task.status.value,
            "passed": False,
        }

    def get_history(self, task_id: uuid.UUID) -> list[ReviewEvent]:
        return list(
            self.session.scalars(
                select(ReviewEvent)
                .where(ReviewEvent.review_task_id == task_id)
                .order_by(ReviewEvent.created_at)
            ).all()
        )
