"""Application errors and helpers for the unified response model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.orchestrator.trace import new_trace_id
from api.schemas.response import ErrorDetail, QueryResponseModel


@dataclass
class AppError(Exception):
    code: str
    message: str
    message_fa: str | None = None
    http_status: int = 500
    details: dict[str, Any] = field(default_factory=dict)

    def to_error_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=self.message,
            message_fa=self.message_fa,
            details=self.details,
        )


class RequestTimeoutError(AppError):
    def __init__(self, *, timeout_seconds: float, trace_id: str | None = None) -> None:
        super().__init__(
            code="REQUEST_TIMEOUT",
            message=f"Query exceeded the {timeout_seconds:g}s time limit.",
            message_fa="زمان پاسخ‌دهی به سؤال از حد مجاز گذشت. لطفاً دوباره تلاش کنید.",
            http_status=504,
            details={"timeout_seconds": timeout_seconds, "trace_id": trace_id},
        )


class EmbeddingTimeoutError(AppError):
    def __init__(self, *, timeout_seconds: float) -> None:
        super().__init__(
            code="EMBEDDING_TIMEOUT",
            message=f"Embedding service timed out after {timeout_seconds:g}s.",
            message_fa="سرویس بازیابی معنایی در زمان مجاز پاسخ نداد.",
            http_status=504,
            details={"timeout_seconds": timeout_seconds},
        )


class ServiceUnavailableError(AppError):
    def __init__(self, *, message: str, message_fa: str | None = None) -> None:
        super().__init__(
            code="SERVICE_UNAVAILABLE",
            message=message,
            message_fa=message_fa or "سرویس موقتاً در دسترس نیست.",
            http_status=503,
        )


def query_error_response(
    *,
    intent: str,
    answer: str,
    trace_id: str,
    session_id: str | None,
    code: str,
    message: str,
    message_fa: str | None = None,
    gate_decision: str = "pass",
    requires_clarification: bool = False,
    confidence: float = 0.0,
    details: dict[str, Any] | None = None,
) -> QueryResponseModel:
    return QueryResponseModel(
        success=False,
        answer=answer,
        intent=intent,
        agents=[],
        citations=[],
        confidence=confidence,
        trace_id=trace_id,
        session_id=session_id,
        requires_clarification=requires_clarification,
        gate_decision=gate_decision,  # type: ignore[arg-type]
        error=ErrorDetail(
            code=code,
            message=message,
            message_fa=message_fa,
            details=details or {},
        ),
    )


def internal_error_response(
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    exc: Exception | None = None,
) -> QueryResponseModel:
    tid = trace_id or new_trace_id()
    return query_error_response(
        intent="SYSTEM.ERROR",
        answer="خطای داخلی سامانه. لطفاً بعداً دوباره تلاش کنید.",
        trace_id=tid,
        session_id=session_id,
        code="INTERNAL_ERROR",
        message=str(exc) if exc else "Internal server error",
        message_fa="خطای داخلی سامانه. لطفاً بعداً دوباره تلاش کنید.",
        gate_decision="pass",
        confidence=0.0,
        details={"exception_type": type(exc).__name__ if exc else None},
    )
