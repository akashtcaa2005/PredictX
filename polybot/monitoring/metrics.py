"""In-memory metrics: counters, gauges, and latency summaries.

Thread-safe so the strategy worker thread and the asyncio loop can both record.
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """Lightweight metrics registry."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _gauges: dict[str, float] = field(default_factory=dict)
    _latencies: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=256))
    )

    def incr(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += amount

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe_latency(self, name: str, millis: float) -> None:
        with self._lock:
            self._latencies[name].append(millis)

    def counter(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, 0.0)

    def latency_stats(self, name: str) -> dict[str, float]:
        with self._lock:
            samples = sorted(self._latencies.get(name, ()))
        if not samples:
            return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        n = len(samples)
        return {
            "count": n,
            "avg_ms": round(sum(samples) / n, 2),
            "p50_ms": round(samples[int(0.50 * (n - 1))], 2),
            "p95_ms": round(samples[int(0.95 * (n - 1))], 2),
            "max_ms": round(samples[-1], 2),
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            latency_names = list(self._latencies)
        return {
            "counters": counters,
            "gauges": gauges,
            "latency": {name: self.latency_stats(name) for name in latency_names},
        }
