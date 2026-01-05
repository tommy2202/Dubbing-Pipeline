from __future__ import annotations

import json

import click

from anime_v2.review.ops import resolve_job_dir
from anime_v2.review.overrides import apply_overrides, load_overrides, save_overrides


@click.group()
def overrides() -> None:
    """Per-job override controls (music regions, speakers, smoothing)."""


@overrides.group("music")
def overrides_music() -> None:
    """Override music/singing preserve regions."""


@overrides_music.command("list")
@click.argument("job", type=str)
def music_list(job: str) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    mro = ov.get("music_regions_overrides", {})
    click.echo(json.dumps(mro, indent=2, sort_keys=True))


@overrides_music.command("add")
@click.argument("job", type=str)
@click.option("--start", type=float, required=True)
@click.option("--end", type=float, required=True)
@click.option(
    "--kind",
    type=click.Choice(["music", "singing", "unknown"], case_sensitive=False),
    default="music",
)
@click.option("--confidence", type=float, default=1.0)
@click.option("--reason", type=str, default="user_add")
def music_add(
    job: str, start: float, end: float, kind: str, confidence: float, reason: str
) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    mro = ov.get("music_regions_overrides", {})
    if not isinstance(mro, dict):
        mro = {"adds": [], "removes": [], "edits": []}
    adds = mro.get("adds", [])
    if not isinstance(adds, list):
        adds = []
    adds.append(
        {
            "start": float(start),
            "end": float(end),
            "kind": str(kind).lower(),
            "confidence": float(confidence),
            "reason": str(reason),
        }
    )
    mro["adds"] = adds
    ov["music_regions_overrides"] = mro
    save_overrides(job_dir, ov)
    click.echo("OK")


@overrides_music.command("remove")
@click.argument("job", type=str)
@click.option("--start", type=float, required=True)
@click.option("--end", type=float, required=True)
@click.option("--reason", type=str, default="user_remove")
def music_remove(job: str, start: float, end: float, reason: str) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    mro = ov.get("music_regions_overrides", {})
    if not isinstance(mro, dict):
        mro = {"adds": [], "removes": [], "edits": []}
    rem = mro.get("removes", [])
    if not isinstance(rem, list):
        rem = []
    rem.append({"start": float(start), "end": float(end), "reason": str(reason)})
    mro["removes"] = rem
    ov["music_regions_overrides"] = mro
    save_overrides(job_dir, ov)
    click.echo("OK")


@overrides_music.command("edit")
@click.argument("job", type=str)
@click.option("--from-start", type=float, required=True)
@click.option("--from-end", type=float, required=True)
@click.option("--start", type=float, required=True)
@click.option("--end", type=float, required=True)
@click.option("--kind", type=str, default="")
@click.option("--confidence", type=float, default=None)
@click.option("--reason", type=str, default="user_edit")
def music_edit(
    job: str,
    from_start: float,
    from_end: float,
    start: float,
    end: float,
    kind: str,
    confidence: float | None,
    reason: str,
) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    mro = ov.get("music_regions_overrides", {})
    if not isinstance(mro, dict):
        mro = {"adds": [], "removes": [], "edits": []}
    edits = mro.get("edits", [])
    if not isinstance(edits, list):
        edits = []
    to = {"start": float(start), "end": float(end), "reason": str(reason)}
    if str(kind).strip():
        to["kind"] = str(kind).lower().strip()
    if confidence is not None:
        to["confidence"] = float(confidence)
    edits.append({"from": {"start": float(from_start), "end": float(from_end)}, "to": to})
    mro["edits"] = edits
    ov["music_regions_overrides"] = mro
    save_overrides(job_dir, ov)
    click.echo("OK")


@overrides.group("speaker")
def overrides_speaker() -> None:
    """Override segment speaker/character IDs."""


@overrides_speaker.command("set")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
@click.argument("character_id", type=str)
def speaker_set(job: str, segment_id: int, character_id: str) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    sp = ov.get("speaker_overrides", {})
    if not isinstance(sp, dict):
        sp = {}
    sp[str(int(segment_id))] = str(character_id).strip()
    ov["speaker_overrides"] = sp
    save_overrides(job_dir, ov)
    click.echo("OK")


@overrides_speaker.command("unset")
@click.argument("job", type=str)
@click.argument("segment_id", type=int)
def speaker_unset(job: str, segment_id: int) -> None:
    job_dir = resolve_job_dir(job)
    ov = load_overrides(job_dir)
    sp = ov.get("speaker_overrides", {})
    if isinstance(sp, dict):
        sp.pop(str(int(segment_id)), None)
    ov["speaker_overrides"] = sp
    save_overrides(job_dir, ov)
    click.echo("OK")


@overrides.command("apply")
@click.argument("job", type=str)
def overrides_apply(job: str) -> None:
    """
    Write effective artifacts + manifest for overrides.

    This is deterministic and safe (no deletes). The pipeline will consume these
    on the next run / re-synthesis.
    """
    job_dir = resolve_job_dir(job)
    rep = apply_overrides(job_dir)
    click.echo(json.dumps(rep.to_dict(), indent=2, sort_keys=True))
