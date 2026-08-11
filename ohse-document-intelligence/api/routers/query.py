"""POST /query API — production-hardened."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from agents.orchestrator.pipeline import QueryOrchestrator
from agents.orchestrator.trace import new_trace_id
from api.errors import AppError, RequestTimeoutError, internal_error_response, query_error_response
from api.schemas.response import QueryResponseModel
from config.settings import get_settings
from database.session import SessionLocal
from security.domain_gate import MAX_QUERY_LENGTH

_query_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="query-worker")


class QueryRequest(BaseModel):
    session_id: str | None = None
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)

    @field_validator("query")
    @classmethod
    def strip_query(cls, v: str) -> str:
        return v.strip()


def _run_query_in_thread(query: str, session_id: str | None):
    db = SessionLocal()
    try:
        orchestrator = QueryOrchestrator(db)
        return orchestrator.handle(query, session_id=session_id)
    finally:
        db.close()


def _to_response_model(result, *, session_id: str | None) -> QueryResponseModel:
    data = result.to_dict()
    gate = data.get("gate_decision", "pass")
    is_gate_failure = gate in ("reject", "block")
    is_system_error = data.get("intent", "").startswith("SYSTEM.")
    success = not is_gate_failure and not is_system_error and data.get("error") is None
    return QueryResponseModel(
        success=success,
        answer=data["answer"],
        intent=data["intent"],
        agents=data["agents"],
        citations=data["citations"],
        confidence=data["confidence"],
        trace_id=data["trace_id"],
        session_id=session_id,
        requires_clarification=data.get("requires_clarification", False),
        gate_decision=gate,
        error=data.get("error"),
    )


def execute_query(db: Session, request: QueryRequest) -> QueryResponseModel:
    """Run query pipeline with a hard timeout and unified error envelope."""
    del db  # each worker thread opens its own DB session
    settings = get_settings()
    trace_id = new_trace_id()
    timeout = settings.query_timeout_seconds
    future = _query_executor.submit(_run_query_in_thread, request.query, request.session_id)
    try:
        result = future.result(timeout=timeout)
        return _to_response_model(result, session_id=request.session_id)
    except FuturesTimeoutError as exc:
        future.cancel()
        err = RequestTimeoutError(timeout_seconds=timeout, trace_id=trace_id)
        return query_error_response(
            intent="SYSTEM.TIMEOUT",
            answer=err.message_fa or err.message,
            trace_id=trace_id,
            session_id=request.session_id,
            code=err.code,
            message=err.message,
            message_fa=err.message_fa,
            gate_decision="pass",
            details=err.details,
        )
    except AppError as exc:
        return query_error_response(
            intent="SYSTEM.ERROR",
            answer=exc.message_fa or exc.message,
            trace_id=trace_id,
            session_id=request.session_id,
            code=exc.code,
            message=exc.message,
            message_fa=exc.message_fa,
            gate_decision="pass",
            details=exc.details,
        )
    except Exception as exc:
        return internal_error_response(
            trace_id=trace_id,
            session_id=request.session_id,
            exc=exc,
        )
