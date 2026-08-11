"""Task claim/release with lease-based concurrency control."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.models import ReviewEventType, ReviewTask, ReviewTaskStatus, ReviewerType
from review.review_events import log_event


class ClaimError(Exception):
    pass


class ReviewAssignment:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def _lease_minutes(self) -> int:
        return self.settings.review_claim_lease_minutes

    def expire_stale_claims(self) -> int:
        now = datetime.now(timezone.utc)
        stale = self.session.scalars(
            select(ReviewTask).where(
                ReviewTask.status == ReviewTaskStatus.IN_PROGRESS,
                ReviewTask.claim_expires_at.isnot(None),
                ReviewTask.claim_expires_at < now,
            )
        ).all()
        for task in stale:
            task.status = ReviewTaskStatus.PENDING
            task.assigned_to = None
            task.reviewer_type = None
            task.claimed_at = None
            task.claim_expires_at = None
            log_event(
                self.session,
                review_task_id=task.id,
                event_type=ReviewEventType.TASK_RELEASED,
                actor_id="system",
                actor_type=ReviewerType.SYSTEM,
                metadata={"reason": "lease_expired"},
            )
        return len(stale)

    def claim(
        self,
        task_id: uuid.UUID,
        reviewer_id: str,
        *,
        reviewer_type: ReviewerType = ReviewerType.HUMAN,
    ) -> ReviewTask:
        self.expire_stale_claims()
        task = self.session.get(ReviewTask, task_id, with_for_update=True)
        if not task:
            raise ClaimError("Task not found")
        if task.status == ReviewTaskStatus.IN_PROGRESS:
            if task.assigned_to == reviewer_id:
                task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=self._lease_minutes())
                return task
            raise ClaimError(f"Task already claimed by {task.assigned_to}")
        if task.status not in {ReviewTaskStatus.PENDING}:
            raise ClaimError(f"Task cannot be claimed in status {task.status.value}")

        now = datetime.now(timezone.utc)
        task.status = ReviewTaskStatus.IN_PROGRESS
        task.assigned_to = reviewer_id
        task.reviewer_type = reviewer_type.value
        task.claimed_at = now
        task.claim_expires_at = now + timedelta(minutes=self._lease_minutes())

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.TASK_CLAIMED,
            actor_id=reviewer_id,
            actor_type=reviewer_type,
            metadata={"expires_at": task.claim_expires_at.isoformat()},
        )
        return task

    def release(self, task_id: uuid.UUID, reviewer_id: str) -> ReviewTask:
        task = self.session.get(ReviewTask, task_id, with_for_update=True)
        if not task:
            raise ClaimError("Task not found")
        if task.assigned_to and task.assigned_to != reviewer_id:
            raise ClaimError("Only the assigned reviewer can release this task")

        task.status = ReviewTaskStatus.PENDING
        task.assigned_to = None
        task.reviewer_type = None
        task.claimed_at = None
        task.claim_expires_at = None

        log_event(
            self.session,
            review_task_id=task.id,
            event_type=ReviewEventType.TASK_RELEASED,
            actor_id=reviewer_id,
            actor_type=ReviewerType.HUMAN,
        )
        return task
