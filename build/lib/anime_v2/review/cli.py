from __future__ import annotations

import json
from pathlib import Path

import click

from anime_v2.review.ops import (
    edit_segment,
    init_review,
    lock_segment,
    play_segment,
    regen_segment,
    render,
    resolve_job_dir,
    unlock_segment,
)
from anime_v2.review.state import load_state, short_preview


@click.group()
def review() -> None:
    """Tier-2B review loop commands."""


@review.command("init")
@click.argument("input_video", type=click.Path(dir_okay=False, path_type=Path))
def review_init(input_video: Path) -> None:
    """
    Initialize Output/<stem>/review/state.json from existing job artifacts.
    """
    from anime_v2.utils.paths import output_dir_for

    job_dir = output_dir_for(Path(input_video))
    p = init_review(job_dir, video_path=Path(input_video))
    click.echo(str(p))


@review.command("list")
@click.argument("job", type=str)
def review_list(job: str) -> None:
    job_dir = resolve_job_dir(job)
    st = load_state(job_dir)
    segs = st.get("segments", [])
    if not isinstance(segs, list):
        segs = []
    for s in segs:
        if not isinstance(s, dict):
            continue
        sid = int(s.get("segment_id") or 0)
        status = str(s.get("status") or "")
        txt = short_preview(str(s.get("chosen_text") or ""))
        click.echo(f"{sid:04d} {status:10s} {txt}")


@review.command("show")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def review_show(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    st = load_state(job_dir)
    segs = st.get("segments", [])
    if not isinstance(segs, list):
        raise click.ClickException("Invalid review state")
    for s in segs:
        if isinstance(s, dict) and int(s.get("segment_id") or 0) == int(segment_id):
            click.echo(json.dumps(s, indent=2, sort_keys=True))
            return
    raise click.ClickException(f"segment_id {segment_id} not found")


@review.command("edit")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
@click.option("--text", required=True, type=str)
def review_edit(job: str, segment_id: int, text: str) -> None:
    job_dir = resolve_job_dir(job)
    edit_segment(job_dir, int(segment_id), text=text)
    click.echo("OK")


@review.command("regen")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def review_regen(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    p = regen_segment(job_dir, int(segment_id))
    click.echo(str(p))


@review.command("play")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def review_play(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    p = play_segment(job_dir, int(segment_id))
    click.echo(str(p))


@review.command("lock")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def review_lock(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    lock_segment(job_dir, int(segment_id))
    click.echo("OK")


@review.command("unlock")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def review_unlock(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    unlock_segment(job_dir, int(segment_id))
    click.echo("OK")


@review.command("render")
@click.argument("job", type=str)
def review_render(job: str) -> None:
    job_dir = resolve_job_dir(job)
    outs = render(job_dir)
    for k, v in outs.items():
        click.echo(f"{k}={v}")

