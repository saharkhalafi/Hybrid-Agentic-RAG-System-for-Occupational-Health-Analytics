"""In-process TTL caches for embeddings and query responses."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """Thread-safe LRU-ish TTL cache with max size."""

    def __init__(self, *, max_size: int = 1024, ttl_seconds: float = 3600.0) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, _CacheEntry[T]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> T | None:
        k = self._key(text)
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(k)
            if entry is None or entry.expires_at <= now:
                self.misses += 1
                if entry is not None:
                    del self._store[k]
                return None
            self.hits += 1
            return entry.value

    def put(self, text: str, value: T) -> None:
        k = self._key(text)
        now = time.monotonic()
        with self._lock:
            if len(self._store) >= self.max_size:
                # Evict oldest expired or first entry
                expired = [kk for kk, e in self._store.items() if e.expires_at <= now]
                for kk in expired:
                    del self._store[kk]
                if len(self._store) >= self.max_size:
                    oldest = next(iter(self._store))
                    del self._store[oldest]
            self._store[k] = _CacheEntry(value=value, expires_at=now + self.ttl_seconds)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


# Module-level singletons (shared across requests in one process)
_embedding_cache: TTLCache[list[float]] | None = None
_query_response_cache: TTLCache[dict[str, Any]] | None = None


def get_embedding_cache(*, max_size: int = 2048, ttl_seconds: float = 7200.0) -> TTLCache[list[float]]:
    global _embedding_cache
    if _embedding_cache is None:
        _embedding_cache = TTLCache(max_size=max_size, ttl_seconds=ttl_seconds)
    return _embedding_cache


def get_query_response_cache(*, max_size: int = 512, ttl_seconds: float = 300.0) -> TTLCache[dict[str, Any]]:
    global _query_response_cache
    if _query_response_cache is None:
        _query_response_cache = TTLCache(max_size=max_size, ttl_seconds=ttl_seconds)
    return _query_response_cache


def reset_caches() -> None:
    global _embedding_cache, _query_response_cache
    if _embedding_cache:
        _embedding_cache.clear()
    if _query_response_cache:
        _query_response_cache.clear()
