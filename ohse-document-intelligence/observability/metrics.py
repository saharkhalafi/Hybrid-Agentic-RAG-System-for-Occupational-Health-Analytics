"""In-process metrics collector for production observability."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Histogram:
    values: list[float] = field(default_factory=list)
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        self.values.append(value)
        self.count += 1
        self.total += value
        # Keep last 1000 for percentile calc
        if len(self.values) > 1000:
            self.values = self.values[-1000:]

    def percentile(self, p: float) -> float:
        if not self.values:
            return 0.0
        sorted_vals = sorted(self.values)
        idx = int(len(sorted_vals) * p / 100)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_ms": round(self.total, 2),
            "avg_ms": round(self.total / self.count, 2) if self.count else 0,
            "p50_ms": round(self.percentile(50), 2),
            "p95_ms": round(self.percentile(95), 2),
        }


class MetricsCollector:
    """Thread-safe in-process metrics. Exportable via /metrics endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, _Histogram] = defaultdict(_Histogram)
        self._start_time = time.monotonic()

    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def observe(self, name: str, value_ms: float) -> None:
        with self._lock:
            self._histograms[name].observe(value_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.monotonic() - self._start_time, 1),
                "counters": dict(self._counters),
                "histograms": {k: h.to_dict() for k, h in self._histograms.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


# Singleton
_collector: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector
