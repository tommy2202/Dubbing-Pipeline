from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt
from anime_v2.utils.ffmpeg_safe import ffprobe_duration_seconds, run_ffmpeg
from anime_v2.utils.log import logger


def _ffprobe_duration_s(path: Path) -> float | None:
    try:
        d = float(ffprobe_duration_seconds(path, timeout_s=20))
        return d if d > 0 else None
    except Exception:
        return None


def mux(
    src_video: Path,
    dub_wav: Path,
    srt_path: Path | None,
    out_mkv: Path,
    *,
    job_id: str | None = None,
) -> Path:
    """
    Mux:
      - video copied (no re-encode)
      - dubbed audio -> AAC 192k
      - soft subtitles (SRT) if provided

    Sync handling:
      - audio starts at t=0
      - audio padded/truncated to match video duration (best-effort)
    """
    out_mkv.parent.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_mkv.parent / ".checkpoint.json"
    if job_id and out_mkv.exists():
        ckpt = read_ckpt(job_id, ckpt_path=ckpt_path)
        if stage_is_done(ckpt, "mux"):
            logger.info("[v2] mux stage checkpoint hit")
            return out_mkv

    vid_dur = _ffprobe_duration_s(src_video)
    if vid_dur is None:
        logger.warning("[v2] ffprobe duration unavailable; mux will be best-effort")

    # If SRT is missing/empty, don't attempt to mux subtitles (ffmpeg fails on empty file).
    if srt_path is not None:
        try:
            if (not srt_path.exists()) or srt_path.stat().st_size == 0:
                logger.warning("[v2] SRT missing/empty; muxing without subtitles (%s)", srt_path)
                srt_path = None
        except Exception:
            srt_path = None

    # Loudness normalize OR at least apply volume. We'll try loudnorm, and if ffmpeg
    # fails (filter missing), retry with volume only.
    def _run(filter_a: str) -> None:
        cmd: list[str] = [
            str(get_settings().ffmpeg_bin),
            "-y",
            "-i",
            str(src_video),
            "-i",
            str(dub_wav),
        ]
        if srt_path is not None:
            cmd += ["-i", str(srt_path)]

        # Map: v from input0, a from input1, optional s from input2
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        if srt_path is not None:
            cmd += ["-map", "2:s:0"]

        cmd += [
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-metadata:s:a:0",
            "language=eng",
            "-filter:a",
            filter_a,
            "-avoid_negative_ts",
            "make_zero",
        ]

        if srt_path is not None:
            cmd += [
                "-c:s",
                "srt",
                "-disposition:s:0",
                "default",
                "-metadata:s:s:0",
                "language=eng",
            ]

        # Force output duration to match the source video (pads audio if short due to apad).
        if vid_dur is not None:
            cmd += ["-t", f"{vid_dur:.3f}"]

        cmd += [str(out_mkv)]
        run_ffmpeg(cmd, timeout_s=900, retries=0, capture=True)

    # Audio filter chain:
    # - loudnorm (1-pass) or fallback to volume
    # - apad to extend if short
    # - atrim to cap at video duration (even if loudnorm stretches slightly)
    # - asetpts resets timestamps to start at 0
    if vid_dur is not None:
        loud_filter = f"loudnorm=I=-16:LRA=11:TP=-1.5:linear=true,apad,atrim=0:{vid_dur:.3f},aresample=async=1:first_pts=0,asetpts=N/SR/TB"
        vol_filter = (
            f"volume=1.0,apad,atrim=0:{vid_dur:.3f},aresample=async=1:first_pts=0,asetpts=N/SR/TB"
        )
    else:
        loud_filter = "loudnorm=I=-16:LRA=11:TP=-1.5:linear=true,apad,aresample=async=1:first_pts=0,asetpts=N/SR/TB"
        vol_filter = "volume=1.0,apad,aresample=async=1:first_pts=0,asetpts=N/SR/TB"

    try:
        _run(loud_filter)
    except Exception as ex:
        logger.warning("[v2] loudnorm failed; retrying with volume filter (%s)", ex)
        _run(vol_filter)

    if job_id:
        with suppress(Exception):
            write_ckpt(
                job_id,
                "mux",
                {"mkv": out_mkv},
                {
                    "work_dir": str(out_mkv.parent),
                    "src_video": str(src_video),
                    "dub_wav": str(dub_wav),
                },
                ckpt_path=ckpt_path,
            )

    logger.info("[v2] mux complete → %s", out_mkv)
    return out_mkv


def run(
    video: Path,
    ckpt_dir: Path,
    out_dir: Path,
    *,
    dubbed_audio: Path | None = None,
    srt_path: Path | None = None,
    mkv_out: Path | None = None,
    **_,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = mkv_out or (out_dir / "dub.mkv")
    if dubbed_audio is None:
        raise ValueError("dubbed_audio is required for mux")
    return mux(src_video=video, dub_wav=dubbed_audio, srt_path=srt_path, out_mkv=out)
