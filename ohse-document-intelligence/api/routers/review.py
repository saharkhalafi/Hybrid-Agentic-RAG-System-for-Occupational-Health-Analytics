"""FastAPI router for human-in-the-loop review workflow."""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.session import get_db
from review.review_assignment import ClaimError
from review.evidence_store import flatten_table_evidence_cells
from review.review_service import ReviewService, ReviewServiceError
from review.schemas import (
    ApproveRequest,
    ClaimRequest,
    CorrectRequest,
    DecisionResponse,
    EvidenceResponse,
    ReviewEventResponse,
    ReviewTaskDetail,
    ReviewTaskListResponse,
    ReviewTaskSummary,
    RejectRequest,
)

router = APIRouter()


def _task_summary(task) -> ReviewTaskSummary:
    return ReviewTaskSummary(
        id=task.id,
        document_id=task.document_id,
        page_number=task.page_number,
        target_type=task.target_type.value,
        target_id=task.target_id,
        stable_table_id=task.stable_table_id,
        primary_issue_code=task.primary_issue_code,
        severity=task.severity,
        title=task.title,
        status=task.status.value,
        priority=task.priority.value,
        assigned_to=task.assigned_to,
        claimed_at=task.claimed_at,
        claim_expires_at=task.claim_expires_at,
        created_at=task.created_at,
        candidate_id=task.candidate_id,
    )


@router.get("/tasks", response_model=ReviewTaskListResponse)
def list_tasks(
    status: str | None = Query(None),
    priority: str | None = Query(None),
    issue_code: str | None = Query(None),
    document_id: uuid.UUID | None = Query(None),
    page_number: int | None = Query(None),
    target_type: str | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ReviewTaskListResponse:
    service = ReviewService(db)
    tasks, total = service.list_tasks(
        status=status,
        priority=priority,
        issue_code=issue_code,
        document_id=document_id,
        page_number=page_number,
        target_type=target_type,
        limit=limit,
        offset=offset,
    )
    return ReviewTaskListResponse(
        tasks=[_task_summary(t) for t in tasks],
        total=total,
    )


@router.get("/tasks/{task_id}", response_model=ReviewTaskDetail)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> ReviewTaskDetail:
    service = ReviewService(db)
    detail = service.get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Task not found")
    task = detail["task"]
    return ReviewTaskDetail(
        **_task_summary(task).model_dump(),
        description=task.description,
        issue_metadata=task.issue_metadata,
        evidence_reference=task.evidence_reference,
        evidence_snapshot=task.evidence_snapshot,
        validation_run_id=task.validation_run_id,
        candidate=detail.get("candidate_payload"),
        validation_report=detail.get("validation_report"),
        machine_issues=detail.get("machine_issues") or [],
    )


@router.post("/tasks/{task_id}/claim", response_model=ReviewTaskSummary)
def claim_task(task_id: uuid.UUID, body: ClaimRequest, db: Session = Depends(get_db)) -> ReviewTaskSummary:
    service = ReviewService(db)
    try:
        task = service.claim_task(task_id, body.reviewer_id)
        db.commit()
        return _task_summary(task)
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/release", response_model=ReviewTaskSummary)
def release_task(task_id: uuid.UUID, body: ClaimRequest, db: Session = Depends(get_db)) -> ReviewTaskSummary:
    service = ReviewService(db)
    try:
        task = service.release_task(task_id, body.reviewer_id)
        db.commit()
        return _task_summary(task)
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/approve", response_model=DecisionResponse)
def approve_task(task_id: uuid.UUID, body: ApproveRequest, db: Session = Depends(get_db)) -> DecisionResponse:
    service = ReviewService(db)
    try:
        result = service.approve_task(task_id, body.reviewer_id, body.comment)
        db.commit()
        return DecisionResponse(**result)
    except ReviewServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/correct", response_model=DecisionResponse)
def correct_task(task_id: uuid.UUID, body: CorrectRequest, db: Session = Depends(get_db)) -> DecisionResponse:
    service = ReviewService(db)
    try:
        result = service.correct_task(
            task_id,
            body.reviewer_id,
            [c.model_dump() for c in body.corrections],
            body.comment,
        )
        db.commit()
        return DecisionResponse(**result)
    except ReviewServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/reject", response_model=DecisionResponse)
def reject_task(task_id: uuid.UUID, body: RejectRequest, db: Session = Depends(get_db)) -> DecisionResponse:
    service = ReviewService(db)
    try:
        result = service.reject_task(task_id, body.reviewer_id, body.reason, body.comment)
        db.commit()
        return DecisionResponse(**result)
    except ReviewServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/history", response_model=list[ReviewEventResponse])
def task_history(task_id: uuid.UUID, db: Session = Depends(get_db)) -> list[ReviewEventResponse]:
    service = ReviewService(db)
    events = service.get_history(task_id)
    return [
        ReviewEventResponse(
            id=e.id,
            event_type=e.event_type.value,
            actor_id=e.actor_id,
            actor_type=e.actor_type.value,
            metadata=e.metadata_,
            created_at=e.created_at,
        )
        for e in events
    ]


def _resolve_pdf_path() -> Path | None:
    settings = get_settings()
    candidates = [
        settings.pdf_source_path,
        Path(settings.gold_dir).parents[1] / "OHE6.pdf",
        Path(__file__).resolve().parents[2].parent / "OHE6.pdf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return Path(path)
    return None


@router.get("/tasks/{task_id}/evidence", response_model=EvidenceResponse)
def task_evidence(task_id: uuid.UUID, db: Session = Depends(get_db)) -> EvidenceResponse:
    service = ReviewService(db)
    detail = service.get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Task not found")
    task = detail["task"]
    payload = detail.get("candidate_payload") or {}
    cells: list[dict[str, Any]] = []
    bboxes: list[dict[str, Any]] = []
    for row in payload.get("rows") or []:
        for fname, fval in row.items():
            if isinstance(fval, dict):
                cells.append(
                    {
                        "field": fname,
                        "value": fval.get("value"),
                        "original_value": fval.get("original_value"),
                        "normalized_value": fval.get("normalized_value"),
                        "cell_id": fval.get("cell_id"),
                        "bbox": fval.get("bbox"),
                    }
                )
                if fval.get("bbox"):
                    bboxes.append({"field": fname, "bbox": fval["bbox"]})

    pdf_path = _resolve_pdf_path()
    page_num = task.page_number
    image_url = f"/review/tasks/{task_id}/evidence/page-image" if pdf_path and page_num else None

    return EvidenceResponse(
        page_number=page_num,
        pdf_available=pdf_path is not None,
        page_image_url=image_url,
        evidence_cells=cells,
        source_table_cells=flatten_table_evidence_cells(task.stable_table_id)
        if task.stable_table_id
        else [],
        candidate_payload=payload,
        bboxes=bboxes,
    )


@router.get("/tasks/{task_id}/evidence/page-image")
def task_page_image(task_id: uuid.UUID, db: Session = Depends(get_db)):
    from fastapi.responses import Response

    service = ReviewService(db)
    task = service.get_task(task_id)
    if not task or not task.page_number:
        raise HTTPException(status_code=404, detail="Task or page not found")

    pdf_path = _resolve_pdf_path()
    if not pdf_path:
        raise HTTPException(status_code=404, detail="PDF source not configured")

    doc = fitz.open(pdf_path)
    page_idx = task.page_number - 1
    if page_idx < 0 or page_idx >= doc.page_count:
        doc.close()
        raise HTTPException(status_code=404, detail="Page out of range")

    page = doc[page_idx]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
    doc.close()
    return Response(content=pix.tobytes("png"), media_type="image/png")
