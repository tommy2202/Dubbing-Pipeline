from __future__ import annotations

import json
import os
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.ops import audit
from dubbing_pipeline.utils.log import _redact_str, safe_log_data


def test_transcript_redaction_default(tmp_path: Path) -> None:
    os.environ["DUBBING_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["LOG_TRANSCRIPTS"] = "0"
    get_settings.cache_clear()

    payload = {"transcript": "hello world", "subtitle": "hi"}
    out = safe_log_data(payload)
    assert isinstance(out, dict)
    assert out["transcript"]["redacted"] is True
    assert out["subtitle"]["redacted"] is True


def test_transcript_logging_enabled(tmp_path: Path) -> None:
    os.environ["DUBBING_LOG_DIR"] = str(tmp_path / "logs2")
    os.environ["LOG_TRANSCRIPTS"] = "1"
    get_settings.cache_clear()

    payload = {"transcript": "hello world"}
    out = safe_log_data(payload)
    assert out["transcript"] == "hello world"


def test_redact_tokens_and_headers() -> None:
    raw = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTYifQ.sig COOKIE=session=abc123 x-api-key=dp_abc_1234567890"
    )
    red = _redact_str(raw)
    assert "***REDACTED***" in red
    assert "eyJ" not in red
    assert "session=abc123" not in red
    assert "dp_" not in red


def test_audit_coarse_fields(tmp_path: Path) -> None:
    os.environ["DUBBING_LOG_DIR"] = str(tmp_path / "logs3")
    get_settings.cache_clear()

    audit.event(
        "privacy.test",
        actor_id="u_123",
        resource_id="job_1",
        request_id="req_123",
        outcome="ok",
        meta_safe={"transcript": "secret", "path": "/tmp/secret.txt", "count": 3},
    )

    log_path = Path(os.environ["DUBBING_LOG_DIR"]) / "audit.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    rec = json.loads(lines[-1])
    assert rec.get("event_type") == "privacy.test"
    assert rec.get("actor_id") == "u_123"
    assert rec.get("resource_id") == "job_1"
    assert rec.get("request_id") == "req_123"
    assert rec.get("outcome") == "ok"
    meta = rec.get("meta") or {}
    assert meta.get("transcript", {}).get("redacted") is True
    assert meta.get("path", {}).get("redacted") is True
