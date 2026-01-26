from __future__ import annotations

import json

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import _redact_str, safe_log_data
from config.settings import get_safe_config_report


def test_secrets_not_in_logs_or_reports(monkeypatch) -> None:
    secret_vals = {
        "JWT_SECRET": "jwt-secret-should-not-log-12345",
        "CSRF_SECRET": "csrf-secret-should-not-log-12345",
        "SESSION_SECRET": "session-secret-should-not-log-12345",
        "API_TOKEN": "api-token-should-not-log-12345",
    }
    for k, v in secret_vals.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()

    raw = " ".join(f"{k}={v}" for k, v in secret_vals.items())
    red = _redact_str(raw)
    for v in secret_vals.values():
        assert v not in red
    assert "***REDACTED***" in red

    report = get_safe_config_report()
    report_text = json.dumps(report)
    for v in secret_vals.values():
        assert v not in report_text


def test_request_header_redaction() -> None:
    payload = {
        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig",
        "cookie": "session=abc123; refresh=def456",
        "x-api-key": "dp_abcdef_ABCDEFGHIJKL",
    }
    red = safe_log_data(payload)
    assert red["authorization"] == "***REDACTED***"
    assert red["cookie"] == "***REDACTED***"
    assert red["x-api-key"] == "***REDACTED***"


def test_query_param_redaction() -> None:
    raw = "https://example.com/?access_token=abc123&api_key=def456&x=1"
    red = _redact_str(raw)
    assert "access_token=***REDACTED***" in red
    assert "api_key=***REDACTED***" in red
