"""FastAPI middleware for production security and observability."""

from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from config.logging import get_logger
from config.settings import get_settings
from observability.metrics import get_metrics
from security.auth import APIKeyAuth
from security.rate_limit import RateLimiter

logger = get_logger(__name__)

_auth: APIKeyAuth | None = None
_rate_limiter: RateLimiter | None = None


def _get_auth() -> APIKeyAuth:
    global _auth
    if _auth is None:
        settings = get_settings()
        _auth = APIKeyAuth(
            key_hashes=settings.api_key_hashes,
            require_auth=settings.api_auth_enabled,
        )
    return _auth


def _get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        settings = get_settings()
        _rate_limiter = RateLimiter(
            rate=settings.rate_limit_rps,
            burst=settings.rate_limit_burst,
        )
    return _rate_limiter


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Inject request ID and structured logging context."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = str(round(elapsed_ms, 1))
        get_metrics().observe("http_latency_ms", elapsed_ms)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiting per API key or client IP."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in ("/health", "/", "/metrics"):
            return await call_next(request)
        limiter = _get_rate_limiter()
        key = request.headers.get("X-API-Key") or request.client.host if request.client else "unknown"
        if not limiter.allow(key):
            get_metrics().inc("rate_limit_exceeded")
            return Response(
                content='{"detail":"Rate limit exceeded"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "1"},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response
