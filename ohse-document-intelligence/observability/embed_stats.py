"""Thread-local embedding stats for per-request cost tracking."""

from __future__ import annotations

import contextvars
from typing import Any

_embed_ctx: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "embed_stats", default=None
)


def reset_embed_stats() -> dict[str, int]:
    stats = {"hits": 0, "misses": 0, "chars": 0}
    _embed_ctx.set(stats)
    return stats


def get_embed_stats() -> dict[str, int]:
    return _embed_ctx.get() or {"hits": 0, "misses": 0, "chars": 0}


def record_cache_hit(text: str) -> None:
    stats = _embed_ctx.get()
    if stats is not None:
        stats["hits"] += 1


def record_cache_miss(text: str) -> None:
    stats = _embed_ctx.get()
    if stats is not None:
        stats["misses"] += 1
        stats["chars"] += len(text)
