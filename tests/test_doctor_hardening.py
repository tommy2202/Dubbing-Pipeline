from __future__ import annotations

from dubbing_pipeline.doctor.container import check_required_secrets
from dubbing_pipeline.utils.doctor_report import format_report_json, format_report_text
from dubbing_pipeline.utils.doctor_types import CheckResult, DoctorReport


def test_report_redacts_secrets(monkeypatch) -> None:
    secret = "supersecret-token-123"
    report = DoctorReport(
        metadata={"timestamp": "2026-01-25T00:00:00Z", "app_version": secret, "git_commit": secret},
        checks=[
            CheckResult(
                id="secret_check",
                name="Secret check",
                status="FAIL",
                details={"value": secret, "nested": {"token": secret}},
                remediation=[f"export API_TOKEN={secret}"],
            )
        ],
    )

    text = format_report_text(report)
    assert secret not in text
    assert "***REDACTED***" in text

    json_out = format_report_json(report)
    json_blob = str(json_out)
    assert secret not in json_blob
    assert "***REDACTED***" in json_blob


def test_change_me_secrets_flagged(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "change-me")
    monkeypatch.setenv("CSRF_SECRET", "change-me")
    monkeypatch.setenv("SESSION_SECRET", "change-me")
    monkeypatch.setenv("API_TOKEN", "change-me")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "change-me")

    res = check_required_secrets()
    assert res.status == "WARN"
    details = res.details or {}
    assert details.get("API_TOKEN") == "default/change-me"
    assert details.get("ADMIN_PASSWORD") == "default/change-me"
