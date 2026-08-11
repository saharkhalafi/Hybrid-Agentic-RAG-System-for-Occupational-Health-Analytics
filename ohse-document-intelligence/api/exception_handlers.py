"""Global FastAPI exception handlers — unified error envelope."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents.orchestrator.trace import new_trace_id
from api.errors import AppError
from api.schemas.response import APIErrorResponse, ErrorDetail


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        body = APIErrorResponse(
            success=False,
            error=exc.to_error_detail(),
            trace_id=str(exc.details.get("trace_id") or new_trace_id()),
        )
        return JSONResponse(status_code=exc.http_status, content=body.model_dump())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        body = APIErrorResponse(
            success=False,
            error=ErrorDetail(
                code=f"HTTP_{exc.status_code}",
                message=str(exc.detail),
            ),
            trace_id=new_trace_id(),
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        body = APIErrorResponse(
            success=False,
            error=ErrorDetail(
                code="VALIDATION_ERROR",
                message="Invalid request payload",
                message_fa="ورودی درخواست نامعتبر است.",
                details={"errors": exc.errors()},
            ),
            trace_id=new_trace_id(),
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        body = APIErrorResponse(
            success=False,
            error=ErrorDetail(
                code="INTERNAL_ERROR",
                message="Internal server error",
                message_fa="خطای داخلی سامانه.",
                details={"exception_type": type(exc).__name__},
            ),
            trace_id=new_trace_id(),
        )
        return JSONResponse(status_code=500, content=body.model_dump())
