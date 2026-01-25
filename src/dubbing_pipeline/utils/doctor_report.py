from __future__ import annotations

import json
from typing import Any

from dubbing_pipeline.utils.doctor_redaction import redact, redact_obj
from dubbing_pipeline.utils.doctor_types import CheckResult, DoctorReport


def _format_details(details: Any) -> list[str]:
    if details is None:
        return []
    safe = redact_obj(details)
    if isinstance(safe, str):
        return safe.splitlines() if safe else []
    try:
        blob = json.dumps(safe, indent=2, sort_keys=True)
        return blob.splitlines()
    except Exception:
        return [redact(str(safe))]


def _format_remediation(remediation: list[str]) -> list[str]:
    lines: list[str] = []
    for cmd in remediation or []:
        c = redact(str(cmd))
        if not c.strip():
            continue
        lines.append(f"$ {c}")
    return lines


def _section(title: str, results: list[CheckResult]) -> list[str]:
    lines = [f"{title} ({len(results)})", "-" * (len(title) + len(str(len(results))) + 3)]
    if not results:
        lines.append("- None")
        return lines
    for r in results:
        lines.append(f"- [{r.id}] {redact(r.name)}")
        for dline in _format_details(r.details):
            lines.append(f"  {dline}")
        rem_lines = _format_remediation(r.remediation)
        if rem_lines:
            lines.append("  Remediation:")
            for rl in rem_lines:
                lines.append(f"    {rl}")
    return lines


def format_report_text(report: DoctorReport) -> str:
    meta = redact_obj(report.metadata or {})
    lines: list[str] = []
    lines.append("Doctor Report")
    lines.append("=" * len(lines[-1]))
    lines.append(f"Timestamp: {redact(str(meta.get('timestamp') or 'unknown'))}")
    lines.append(f"App Version: {redact(str(meta.get('app_version') or 'unknown'))}")
    lines.append(f"Commit: {redact(str(meta.get('git_commit') or 'unknown'))}")
    lines.append("Note: secrets redacted")
    lines.append("")

    failures = [c for c in report.checks if c.status == "FAIL"]
    warnings = [c for c in report.checks if c.status == "WARN"]
    passed = [c for c in report.checks if c.status == "PASS"]

    lines.extend(_section("FAILURES", failures))
    lines.append("")
    lines.extend(_section("WARNINGS", warnings))
    lines.append("")
    lines.extend(_section("PASSED", passed))
    lines.append("")
    return "\n".join(lines)


def format_report_json(report: DoctorReport) -> dict[str, Any]:
    safe_checks: list[dict[str, Any]] = []
    for c in report.checks:
        safe_checks.append(
            {
                "id": redact(c.id),
                "name": redact(c.name),
                "status": c.status,
                "details": redact_obj(c.details),
                "remediation": [redact(r) for r in (c.remediation or [])],
            }
        )
    return {
        "metadata": redact_obj(report.metadata or {}),
        "summary": report.summary(),
        "checks": safe_checks,
        "note": "secrets redacted",
    }
