"""Query router API endpoints — with optional auth."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from api.middleware.security import _get_auth
from api.routers.query import QueryRequest, QueryResponseModel, execute_query
from config.settings import get_settings
from database.session import get_db

router = APIRouter()


def _optional_auth(x_api_key: str | None = Header(None)) -> str | None:
    settings = get_settings()
    if not settings.api_auth_enabled:
        return None
    auth = _get_auth()
    ctx = auth.authenticate(x_api_key)
    if ctx is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return ctx.api_key_id


@router.post("/query", response_model=QueryResponseModel)
def post_query(
    request: QueryRequest,
    db: Session = Depends(get_db),
    _key: str | None = Depends(_optional_auth),
) -> QueryResponseModel:
    return execute_query(db, request)
