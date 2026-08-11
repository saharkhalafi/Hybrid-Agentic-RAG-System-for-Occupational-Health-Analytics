"""FastAPI application entrypoint — production-hardened."""

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.orm import Session

from api.middleware.security import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    _get_auth,
)
from api.exception_handlers import register_exception_handlers
from api.routers import query_router as query_api
from api.routers import review as review_router
from config.logging import configure_logging
from config.settings import get_settings
from database.session import get_db, verify_connection
from observability.metrics import get_metrics

configure_logging()
settings = get_settings()

app = FastAPI(
    title="OHSE Document Intelligence Platform",
    version=settings.processing_version,
    description="Hybrid structured + semantic document intelligence for occupational health documents.",
)
register_exception_handlers(app)

# Production middleware (Phase D)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestContextMiddleware)

app.include_router(review_router.router, prefix="/review", tags=["review"])
app.include_router(query_api.router, tags=["query"])

_ui_dir = Path(__file__).resolve().parents[1] / "review" / "ui"
if _ui_dir.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/review/ui", StaticFiles(directory=str(_ui_dir), html=True), name="review-ui")


def _verify_api_key(x_api_key: str | None = Header(None)) -> str:
    """Dependency: verify API key when auth is enabled."""
    auth = _get_auth()
    ctx = auth.authenticate(x_api_key)
    if ctx is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return ctx.api_key_id


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    verify_connection()
    return {
        "status": "ok",
        "environment": settings.environment,
        "project": settings.gcp_project_id,
        "domain_gate": settings.domain_gate_enabled,
        "auth_enabled": settings.api_auth_enabled,
    }


@app.get("/metrics")
def metrics() -> dict:
    """Production metrics snapshot (counters, histograms, cache stats)."""
    from agents.orchestrator.pipeline import get_shared_session_store
    from cache.ttl_cache import get_embedding_cache, get_query_response_cache

    snap = get_metrics().snapshot()
    snap["session_store"] = get_shared_session_store().stats()
    snap["embedding_cache"] = get_embedding_cache().stats()
    snap["query_response_cache"] = get_query_response_cache().stats()
    return snap


@app.get("/")
def root() -> dict:
    return {
        "service": "ohse-document-intelligence",
        "version": settings.processing_version,
        "review_ui": "/review/ui/",
        "endpoints": {
            "query": "POST /query",
            "health": "GET /health",
            "metrics": "GET /metrics",
        },
    }
