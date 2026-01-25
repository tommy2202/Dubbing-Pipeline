from __future__ import annotations

from pathlib import Path

import click

from dubbing_pipeline.doctor.container import build_container_quick_checks, default_report_path
from dubbing_pipeline.utils.doctor_report import format_report_json, format_report_text
from dubbing_pipeline.utils.doctor_runner import run_checks, write_report
from dubbing_pipeline.utils.doctor_types import CheckResult


def _full_not_implemented() -> CheckResult:
    return CheckResult(
        id="doctor_full",
        name="Full mode (not implemented)",
        status="WARN",
        details="Full mode is not yet implemented. Use --mode quick for now.",
        remediation=[],
    )


@click.command(name="doctor")
@click.option(
    "--write-report",
    "write_report_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write report to this path.",
)
@click.option(
    "--mode",
    type=click.Choice(["quick", "full"], case_sensitive=False),
    default="quick",
    show_default=True,
)
@click.option("--json", "json_flag", is_flag=True, default=False, help="Also write JSON report.")
@click.option(
    "--require-gpu",
    is_flag=True,
    default=False,
    help="Treat missing CUDA as FAIL.",
)
def doctor(write_report_path: Path | None, mode: str, json_flag: bool, require_gpu: bool) -> None:
    """
    Container doctor (quick mode).
    """
    report_path = write_report_path or default_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    mode = str(mode or "quick").strip().lower()
    if mode == "full":
        checks = [_full_not_implemented]
    else:
        checks = build_container_quick_checks(require_gpu=require_gpu)

    report = run_checks(checks)
    text = format_report_text(report)
    json_data = format_report_json(report) if json_flag else None
    write_report(report_path, text=text, json_data=json_data)

    summary = report.summary()
    click.echo(f"Doctor ({mode}): PASS={summary['PASS']} WARN={summary['WARN']} FAIL={summary['FAIL']}")
    click.echo(f"Report: {report_path}")
    if json_flag:
        click.echo(f"Report (json): {report_path}.json")

    if summary["FAIL"] > 0:
        raise SystemExit(2)
