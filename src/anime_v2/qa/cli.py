from __future__ import annotations

import json
from pathlib import Path

import click

from anime_v2.qa.scoring import score_job


@click.group()
def qa() -> None:
    """Quality checks and scoring."""


@qa.command("run")
@click.argument("job", required=True, type=str)
@click.option("--top", "top_n", type=int, default=20, show_default=True)
@click.option("--fail-only", is_flag=True, default=False, show_default=True)
@click.option("--no-write", is_flag=True, default=False, show_default=True)
def qa_run(job: str, top_n: int, fail_only: bool, no_write: bool) -> None:
    """
    Compute QA reports for Output/<job>/.

    JOB can be:
    - a job directory path, or
    - a job name under Output/ (same behavior as review commands).
    """
    summary = score_job(job, enabled=True, write_outputs=(not no_write), top_n=top_n, fail_only=fail_only)
    click.echo(json.dumps(summary, indent=2, sort_keys=True))


@qa.command("show")
@click.argument("job", required=True, type=str)
def qa_show(job: str) -> None:
    """Print existing QA summary.json (if present)."""
    from anime_v2.review.ops import resolve_job_dir

    job_dir = resolve_job_dir(job)
    p = Path(job_dir) / "qa" / "summary.json"
    if not p.exists():
        raise click.ClickException(f"Missing QA summary: {p} (run `anime-v2 qa run {job}`)")
    click.echo(p.read_text(encoding="utf-8", errors="replace"))

