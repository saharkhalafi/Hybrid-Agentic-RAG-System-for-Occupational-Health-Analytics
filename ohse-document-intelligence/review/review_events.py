"""Append-only audit event logging for review workflow."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from database.models import ReviewEvent, ReviewEventType, ReviewerType


def log_event(
    session: Session,
    *,
    review_task_id: uuid.UUID,
    event_type: ReviewEventType,
    actor_id: str,
    actor_type: ReviewerType = ReviewerType.HUMAN,
    metadata: dict[str, Any] | None = None,
) -> ReviewEvent:
    event = ReviewEvent(
        review_task_id=review_task_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type=actor_type,
        metadata_=metadata,
    )
    session.add(event)
    return event
