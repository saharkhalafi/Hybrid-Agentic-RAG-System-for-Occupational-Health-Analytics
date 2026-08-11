"""Pydantic schemas for HITL review API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReviewTaskSummary(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    page_number: int | None
    target_type: str
    target_id: str
    stable_table_id: str | None
    primary_issue_code: str
    severity: str
    title: str
    status: str
    priority: str
    assigned_to: str | None
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    created_at: datetime | None = None
    candidate_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class ReviewTaskDetail(ReviewTaskSummary):
    description: str | None
    issue_metadata: dict[str, Any] | None
    evidence_reference: dict[str, Any] | None
    evidence_snapshot: dict[str, Any] | None
    validation_run_id: uuid.UUID | None
    candidate: dict[str, Any] | None = None
    validation_report: dict[str, Any] | None = None
    machine_issues: list[dict[str, Any]] = Field(default_factory=list)


class ReviewTaskListResponse(BaseModel):
    tasks: list[ReviewTaskSummary]
    total: int


class ClaimRequest(BaseModel):
    reviewer_id: str = Field(..., min_length=1, max_length=256)


class CorrectionItem(BaseModel):
    field_name: str
    original_value: str | None = None
    corrected_value: str | None = None
    correction_type: str = "field_value"
    reason: str | None = None
    target_type: str | None = None
    target_id: str | None = None


class CorrectRequest(BaseModel):
    reviewer_id: str
    comment: str | None = None
    corrections: list[CorrectionItem]


class ApproveRequest(BaseModel):
    reviewer_id: str
    comment: str | None = None


class RejectRequest(BaseModel):
    reviewer_id: str
    reason: str = Field(..., min_length=3)
    comment: str | None = None


class DecisionResponse(BaseModel):
    task_id: uuid.UUID
    decision: str
    new_status: str
    validation_run_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    candidate_status: str | None = None
    passed: bool | None = None


class ReviewEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    actor_id: str
    actor_type: str
    metadata: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EvidenceResponse(BaseModel):
    page_number: int | None
    pdf_available: bool
    page_image_url: str | None
    evidence_cells: list[dict[str, Any]] = Field(default_factory=list)
    source_table_cells: list[dict[str, Any]] = Field(default_factory=list)
    candidate_payload: dict[str, Any] | None = None
    bboxes: list[dict[str, Any]] = Field(default_factory=list)
