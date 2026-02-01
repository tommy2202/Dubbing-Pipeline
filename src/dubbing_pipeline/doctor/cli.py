from __future__ import annotations

from pathlib import Path

import click

from dubbing_pipeline.doctor.wizard import run_doctor


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
    Setup wizard / doctor.
    """
    mode = str(mode or "quick").strip().lower()
    report_path = None
    if write_report_path is not None:
        report_path = Path(write_report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)

    report, txt_path, json_path = run_doctor(
        require_gpu=require_gpu,
        report_path=report_path,
        include_smoke=True,
    )

    summary = report.summary()
    click.echo(
        f"Doctor ({mode}): PASS={summary['PASS']} WARN={summary['WARN']} FAIL={summary['FAIL']}"
    )
    click.echo(f"Report: {txt_path}")
    if not json_flag:
        click.echo("Note: JSON report is always written.")
    click.echo(f"Report (json): {json_path}")

    if summary["FAIL"] > 0:
        raise SystemExit(2)
