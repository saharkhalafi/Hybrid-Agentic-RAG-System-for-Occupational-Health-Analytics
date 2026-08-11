"""Production API response and error schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    message_fa: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class QueryResponseModel(BaseModel):
    """Unified /query response — success and handled failures share the same shape."""

    success: bool = True
    answer: str
    intent: str
    agents: list[str]
    citations: list[dict[str, Any]]
    confidence: float
    trace_id: str
    session_id: str | None = None
    requires_clarification: bool = False
    gate_decision: Literal["pass", "reject", "block", "clarify"] = "pass"
    error: ErrorDetail | None = None


class APIErrorResponse(BaseModel):
    """HTTP-level errors (422, 401, 500, 504) use the same error block."""

    success: bool = False
    error: ErrorDetail
    trace_id: str | None = None
