from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.jobs.models import Job
from anime_v2.library.normalize import normalize_series_title, series_to_slug
from anime_v2.utils.log import logger


def _output_root() -> Path:
    return Path(get_settings().output_dir).resolve()


def _library_root() -> Path:
    # Keep Library under Output so it stays a runtime artifact (and is served by /files/*).
    return (_output_root() / "Library").resolve()


def get_job_output_root(job: Job) -> Path:
    """
    Canonical Output/ base directory for a job (legacy layout, kept for compatibility).

    Rules (preserve existing behavior):
    - Prefer parent of job.output_mkv when it looks valid.
    - Else base is Output/<project.output_subdir?>/<source_stem|video_stem|job_id>/
    """
    out_root = _output_root()

    out_mkv = str(getattr(job, "output_mkv", "") or "").strip()
    if out_mkv:
        with suppress(Exception):
            p = Path(out_mkv)
            if p.parent.exists():
                return p.parent.resolve()

    runtime: dict[str, Any] = {}
    try:
        runtime = dict(job.runtime or {}) if isinstance(job.runtime, dict) else {}
    except Exception:
        runtime = {}

    # Stable naming override for privacy/encrypted uploads.
    stem = str(runtime.get("source_stem") or "").strip()
    if not stem:
        with suppress(Exception):
            stem = Path(str(job.video_path)).stem
    if not stem:
        stem = str(job.id or "job")

    proj_sub = ""
    try:
        proj = runtime.get("project")
        if isinstance(proj, dict):
            proj_sub = str(proj.get("output_subdir") or "").strip().strip("/")
    except Exception:
        proj_sub = ""

    if proj_sub:
        return (out_root / proj_sub / stem).resolve()
    return (out_root / stem).resolve()


def get_library_root_for_job(job: Job) -> Path:
    """
    Canonical grouped library directory for a job.

    Layout (under Output/Library):
      Library/{series_slug}/season-XX/episode-YY/job-{job_id}/
    """
    title = normalize_series_title(str(getattr(job, "series_title", "") or ""))
    slug = str(getattr(job, "series_slug", "") or "").strip()
    if not slug and title:
        slug = series_to_slug(title)
    if not slug:
        # Conservative fallback (legacy/CLI): avoid crashing; keep deterministic.
        slug = "unknown-series"
    try:
        season = int(getattr(job, "season_number", 0) or 0)
    except Exception:
        season = 0
    try:
        episode = int(getattr(job, "episode_number", 0) or 0)
    except Exception:
        episode = 0
    season = max(0, season)
    episode = max(0, episode)

    return (
        _library_root()
        / slug
        / f"season-{season:02d}"
        / f"episode-{episode:02d}"
        / f"job-{str(job.id)}"
    ).resolve()


def ensure_library_dir(job: Job) -> Path | None:
    """
    Best-effort create the grouped library directory tree for a job.
    Returns None on failure (caller must fall back to Output/ only).
    """
    root = get_library_root_for_job(job)
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Ensure expected subdirs exist even when we don't mirror/copy artifacts.
        for sub in ("logs", "qa"):
            with suppress(Exception):
                (root / sub).mkdir(parents=True, exist_ok=True)
        return root
    except Exception as ex:
        logger.warning("library_dir_create_failed", path=str(root), error=str(ex))
        return None


def _try_link_file(dst: Path, src: Path) -> bool:
    """
    Best-effort "mirror" link creation.
    Order:
    - hardlink (no admin on Windows, no extra disk)
    - symlink (may require elevated perms on Windows)
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    if not src.exists() or not src.is_file():
        return False
    if dst.exists():
        return True
    try:
        os.link(str(src), str(dst))
        return True
    except Exception:
        pass
    try:
        dst.symlink_to(src)
        return True
    except Exception:
        return False


def mirror_outputs_best_effort(
    *,
    job: Job,
    library_dir: Path,
    master: Path | None,
    mobile: Path | None,
    hls_index: Path | None,
    output_dir: Path,
) -> None:
    """
    Best-effort attempt to make the Library/ directory contain the expected filenames.

    This avoids breaking Windows by preferring hardlinks, and falls back to
    leaving the directory as an index-only view when linking isn't possible.
    """
    # master.mkv + mobile.mp4
    try:
        if master is not None:
            _try_link_file(library_dir / "master.mkv", Path(master))
    except Exception:
        pass
    try:
        if mobile is not None:
            _try_link_file(library_dir / "mobile.mp4", Path(mobile))
    except Exception:
        pass

    # HLS directory (optional). If we cannot link the whole tree, write a pointer file.
    try:
        if hls_index is not None:
            hls_index = Path(hls_index)
            if hls_index.exists():
                hls_src_dir = hls_index.parent
                hls_dst_dir = library_dir / "hls"
                hls_dst_dir.mkdir(parents=True, exist_ok=True)
                linked_any = False
                with suppress(Exception):
                    for p in hls_src_dir.iterdir():
                        if not p.is_file():
                            continue
                        if _try_link_file(hls_dst_dir / p.name, p):
                            linked_any = True
                if not linked_any:
                    (hls_dst_dir / "target.txt").write_text(
                        str(hls_src_dir.resolve()) + "\n", encoding="utf-8"
                    )
    except Exception:
        pass

    # Always write a pointer to the canonical Output dir for tooling.
    try:
        (library_dir / "output_target.txt").write_text(
            str(Path(output_dir).resolve()) + "\n", encoding="utf-8"
        )
    except Exception:
        pass

