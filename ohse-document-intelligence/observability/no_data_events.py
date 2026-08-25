"""Persist no-data events for HITL demand prioritization."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from agents.structured.no_data_reason import NoDataReason
from config.logging import get_logger
from database.models import NoDataEvent

logger = get_logger(__name__)


def record_no_data_event(
    session: Session,
    *,
    reason: NoDataReason,
    query: str,
    trace_id: str | None = None,
    chemical_id: str | None = None,
    cas: str | None = None,
    intent: str | None = None,
) -> None:
    if reason != NoDataReason.PENDING_PROMOTION:
        return
    try:
        event = NoDataEvent(
            id=uuid.uuid4(),
            reason=reason.value,
            query_text=(query or "")[:2000],
            trace_id=trace_id,
            chemical_id=uuid.UUID(chemical_id) if chemical_id else None,
            cas=cas,
            intent=intent,
        )
        session.add(event)
        session.flush()
    except Exception as exc:
        logger.warning("no_data_event_record_failed", error=str(exc), reason=reason.value)


def pending_promotion_demand_summary(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    """Top chemicals by legacy-only no_data demand (HITL backlog prioritization)."""
    from sqlalchemy import func, select

    rows = session.execute(
        select(
            NoDataEvent.chemical_id,
            NoDataEvent.cas,
            func.count().label("request_count"),
        )
        .where(NoDataEvent.reason == NoDataReason.PENDING_PROMOTION.value)
        .group_by(NoDataEvent.chemical_id, NoDataEvent.cas)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [
        {
            "chemical_id": str(chemical_id) if chemical_id else None,
            "cas": cas,
            "request_count": int(request_count),
        }
        for chemical_id, cas, request_count in rows
    ]
