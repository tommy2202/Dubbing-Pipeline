from __future__ import annotations

import shutil
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.ffmpeg_safe import FFmpegError, run_ffmpeg
from dubbing_pipeline.utils.log import logger


def preview_paths(base_dir: Path) -> dict[str, Path]:
    base = Path(base_dir).resolve()
    pdir = (base / "preview").resolve()
    return {
        "dir": pdir,
        "audio": pdir / "preview_audio.m4a",
        "video": pdir / "preview_video.mp4",
    }


def _ffmpeg_available() -> bool:
    s = get_settings()
    ff = Path(str(s.ffmpeg_bin))
    if ff.is_file():
        return True
    return bool(shutil.which(str(ff)))


def _ensure_source(path: Path) -> Path:
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    return p


def _is_fresh(src: Path, dst: Path) -> bool:
    try:
        if not dst.exists():
            return False
        return float(dst.stat().st_mtime) >= float(src.stat().st_mtime)
    except Exception:
        return False


def generate_audio_preview(input_video: Path, out_audio: Path) -> None:
    """
    Generate an audio-only preview (AAC in M4A container).
    """
    if not _ffmpeg_available():
        raise FFmpegError("ffmpeg unavailable")
    src = _ensure_source(input_video)
    out_audio = Path(out_audio).resolve()
    out_audio.parent.mkdir(parents=True, exist_ok=True)
    if _is_fresh(src, out_audio):
        return
    s = get_settings()
    cmd = [
        str(s.ffmpeg_bin),
        "-y",
        "-i",
        str(src),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        str(out_audio),
    ]
    run_ffmpeg(cmd, timeout_s=300, retries=0, capture=True)


def generate_lowres_preview(input_video: Path, out_video: Path) -> None:
    """
    Generate a low-res MP4 preview (<=480p).
    """
    if not _ffmpeg_available():
        raise FFmpegError("ffmpeg unavailable")
    src = _ensure_source(input_video)
    out_video = Path(out_video).resolve()
    out_video.parent.mkdir(parents=True, exist_ok=True)
    if _is_fresh(src, out_video):
        return
    s = get_settings()
    cmd = [
        str(s.ffmpeg_bin),
        "-y",
        "-i",
        str(src),
        "-vf",
        "scale=-2:480:force_original_aspect_ratio=decrease",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-maxrate",
        "800k",
        "-bufsize",
        "1600k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(out_video),
    ]
    run_ffmpeg(cmd, timeout_s=600, retries=0, capture=True)


def log_preview_skip(kind: str, *, job_id: str, reason: str) -> None:
    logger.warning("preview_unavailable", job_id=str(job_id), kind=str(kind), reason=str(reason))
