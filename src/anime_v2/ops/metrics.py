from __future__ import annotations

import time
from contextlib import contextmanager
from collections.abc import Callable, Iterator

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

# Standard latency buckets (seconds) for long-ish media pipeline stages.
PIPELINE_BUCKETS = (
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    900.0,
    1200.0,
    1800.0,
    3600.0,
)

# Jobs
jobs_queued = Counter("anime_v2_jobs_queued_total", "Jobs queued", registry=REGISTRY)
jobs_finished = Counter(
    "anime_v2_jobs_finished_total",
    "Jobs finished by final state",
    labelnames=("state",),
    registry=REGISTRY,
)
job_errors = Counter("anime_v2_job_errors_total", "Job stage errors", labelnames=("stage",), registry=REGISTRY)

# Stage durations
tts_seconds = Histogram("anime_v2_tts_seconds", "TTS stage seconds", registry=REGISTRY, buckets=PIPELINE_BUCKETS)
whisper_seconds = Histogram("anime_v2_whisper_seconds", "Whisper stage seconds", registry=REGISTRY, buckets=PIPELINE_BUCKETS)

# Requested pipeline metrics (explicit names for dashboards)
pipeline_transcribe_seconds = Histogram(
    "pipeline_transcribe_seconds",
    "Pipeline transcribe stage latency (seconds)",
    registry=REGISTRY,
    buckets=PIPELINE_BUCKETS,
)
pipeline_tts_seconds = Histogram(
    "pipeline_tts_seconds",
    "Pipeline TTS stage latency (seconds)",
    registry=REGISTRY,
    buckets=PIPELINE_BUCKETS,
)
pipeline_mux_seconds = Histogram(
    "pipeline_mux_seconds",
    "Pipeline mux/mix stage latency (seconds)",
    registry=REGISTRY,
    buckets=PIPELINE_BUCKETS,
)

pipeline_job_total = Counter("pipeline_job_total", "Pipeline jobs created", registry=REGISTRY)
pipeline_job_failed_total = Counter("pipeline_job_failed_total", "Pipeline jobs failed", registry=REGISTRY)
pipeline_job_degraded_total = Counter("pipeline_job_degraded_total", "Pipeline jobs marked degraded", registry=REGISTRY)


@contextmanager
def time_hist(h: Histogram) -> Iterator[Callable[[], float]]:
    """
    Context manager to time a block and observe into a histogram.
    Usage:
        with time_hist(hist) as elapsed:
            ...
        dt = elapsed()
    """
    t0 = time.perf_counter()
    dt: float | None = None

    def elapsed() -> float:
        return float(dt or 0.0)

    try:
        yield elapsed
    finally:
        dt = max(0.0, time.perf_counter() - t0)
        try:
            h.observe(dt)
        except Exception:
            pass

