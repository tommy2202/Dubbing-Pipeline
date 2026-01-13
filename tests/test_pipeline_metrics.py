from __future__ import annotations

import os

from fastapi.testclient import TestClient

from dubbing_pipeline.server import app


def test_metrics_exposes_pipeline_histograms() -> None:
    # Ensure /metrics is enabled
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    with TestClient(app) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        body = r.text
        assert "pipeline_transcribe_seconds_bucket" in body
        assert "pipeline_tts_seconds_bucket" in body
        assert "pipeline_mux_seconds_bucket" in body
        assert "pipeline_job_total" in body
        assert "pipeline_job_failed_total" in body
        assert "pipeline_job_degraded_total" in body
