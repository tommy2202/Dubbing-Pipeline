from __future__ import annotations

from pathlib import Path

import click

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.plugins.lipsync.preview import preview_lipsync_ranges, write_preview_report
from dubbing_pipeline.utils.paths import output_dir_for


@click.group(help="Lip-sync tools (optional).")
def lipsync() -> None:
    pass


@lipsync.command(
    "preview", help="Preview face visibility and recommend lip-sync ranges (offline, best-effort)."
)
@click.argument("video", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--out-dir", type=click.Path(path_type=Path), default=None, show_default=False)
@click.option(
    "--sample-every",
    "sample_every_s",
    type=float,
    default=float(get_settings().lipsync_sample_every_s),
    show_default=True,
    help="Seconds between sampled frames (lower = more accurate, slower).",
)
@click.option(
    "--max-frames",
    type=int,
    default=int(get_settings().lipsync_max_frames),
    show_default=True,
    help="Upper bound on frames extracted for preview.",
)
@click.option(
    "--min-face-ratio",
    type=float,
    default=float(get_settings().lipsync_min_face_ratio),
    show_default=True,
    help="Minimum face-detection hit ratio to recommend a range.",
)
@click.option(
    "--min-range",
    "min_range_s",
    type=float,
    default=float(get_settings().lipsync_min_range_s),
    show_default=True,
    help="Minimum duration (seconds) for a recommended range.",
)
@click.option(
    "--merge-gap",
    "merge_gap_s",
    type=float,
    default=float(get_settings().lipsync_merge_gap_s),
    show_default=True,
    help="Merge small face-detection gaps up to this many seconds.",
)
def preview(
    video: Path,
    out_dir: Path | None,
    sample_every_s: float,
    max_frames: int,
    min_face_ratio: float,
    min_range_s: float,
    merge_gap_s: float,
) -> None:
    video = Path(video).resolve()
    if out_dir is None:
        out_dir = output_dir_for(video)
    out_dir = Path(out_dir).resolve()
    tmp = out_dir / "tmp" / "lipsync_preview"
    rep = preview_lipsync_ranges(
        video=video,
        work_dir=tmp,
        sample_every_s=float(sample_every_s),
        max_frames=int(max_frames),
        min_face_ratio=float(min_face_ratio),
        min_range_s=float(min_range_s),
        merge_gap_s=float(merge_gap_s),
    )
    analysis_dir = out_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    out_path = write_preview_report(rep, out_path=analysis_dir / "lipsync_preview.json")
    click.echo(str(out_path))
