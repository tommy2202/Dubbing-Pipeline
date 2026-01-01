from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

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
tts_seconds = Histogram("anime_v2_tts_seconds", "TTS stage seconds", registry=REGISTRY)
whisper_seconds = Histogram("anime_v2_whisper_seconds", "Whisper stage seconds", registry=REGISTRY)

