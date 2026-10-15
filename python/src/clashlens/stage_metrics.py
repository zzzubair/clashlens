"""How long each worker stage takes, for the worker's metrics page."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from time import monotonic, thread_time
from typing import Any

STAGE_DURATION_BUCKETS_SECONDS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class StageMetrics:
    """Bounded thread-safe worker stage histograms for production evidence."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stages: dict[str, dict[str, Any]] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started, cpu_started = monotonic(), thread_time()
        try:
            yield
        finally:
            cpu_seconds = max(0.0, thread_time() - cpu_started)
            self.record(stage, max(0.0, monotonic() - started), cpu_seconds)

    def record(
        self, stage: str, duration_seconds: float, cpu_seconds: float | None = None
    ) -> None:
        with self._lock:
            values = self._stages.setdefault(
                stage,
                {
                    "count": 0,
                    "sum_seconds": 0.0,
                    # Only job stages are measured with thread time, never a mix.
                    "thread_cpu_seconds": None,
                    "buckets": [0] * (len(STAGE_DURATION_BUCKETS_SECONDS) + 1),
                },
            )
            values["count"] += 1
            values["sum_seconds"] += duration_seconds
            if cpu_seconds is not None:
                values["thread_cpu_seconds"] = (
                    values["thread_cpu_seconds"] or 0.0
                ) + cpu_seconds
            for index, upper_bound in enumerate(STAGE_DURATION_BUCKETS_SECONDS):
                if duration_seconds <= upper_bound:
                    values["buckets"][index] += 1
            values["buckets"][-1] += 1

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            copied = {
                stage: {
                    "count": values["count"],
                    "sum_seconds": values["sum_seconds"],
                    "thread_cpu_seconds": values["thread_cpu_seconds"],
                    "buckets": list(values["buckets"]),
                }
                for stage, values in self._stages.items()
            }
        report: dict[str, dict[str, float | int | None]] = {}
        for stage, values in sorted(copied.items()):
            count = int(values["count"])
            buckets = values["buckets"]

            def percentile(
                fraction: float, *, count: int = count, buckets: list[int] = buckets
            ) -> float | None:
                rank = count * fraction
                for index, bucket_count in enumerate(buckets):
                    if bucket_count >= rank:
                        if index == len(STAGE_DURATION_BUCKETS_SECONDS):
                            return None
                        return STAGE_DURATION_BUCKETS_SECONDS[index] * 1000
                return None

            report[stage] = {
                "count": count,
                "elapsed_seconds": values["sum_seconds"],
                "thread_cpu_seconds": values["thread_cpu_seconds"],
                "average_ms": float(values["sum_seconds"]) * 1000 / count,
                "p50_upper_ms": percentile(0.50),
                "p95_upper_ms": percentile(0.95),
                "p99_upper_ms": percentile(0.99),
            }
        return report
