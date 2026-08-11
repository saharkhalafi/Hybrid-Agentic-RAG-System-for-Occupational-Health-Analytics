"""Human review queue — legacy DB path; production HITL uses review_service."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from database.models import ReviewIssueType, ReviewQueueItem, ReviewStatus


def enqueue_review(
    session: Session,
    *,
    object_type: str,
    object_id: uuid.UUID,
    document_id: uuid.UUID,
    issue_type: ReviewIssueType,
    page_number: int | None = None,
    bbox: dict | None = None,
    confidence: float | None = None,
    review_result: dict | None = None,
) -> ReviewQueueItem:
    """Legacy enqueue for process_document.py — new pipeline uses review.task_factory."""
    item = ReviewQueueItem(
        object_type=object_type,
        object_id=object_id,
        document_id=document_id,
        page_number=page_number,
        bbox=bbox,
        issue_type=issue_type,
        confidence=confidence,
        status=ReviewStatus.PENDING,
        review_result=review_result,
    )
    session.add(item)
    return item
