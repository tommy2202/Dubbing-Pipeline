from __future__ import annotations

import subprocess
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.utils.log import logger


def _srt_ok(srt: Path | None) -> Path | None:
    if srt is None:
        return None
    try:
        if not srt.exists() or srt.stat().st_size == 0:
            return None
    except Exception:
        return None
    return srt


def export_mkv(video_in: Path, dub_wav: Path, srt: Path | None, out_path: Path) -> Path:
    """
    MKV export (existing behavior):
      - video copied when possible
      - audio -> AAC 192k
      - subtitles: SRT soft subs (optional)
    """
    video_in = Path(video_in)
    dub_wav = Path(dub_wav)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    srt = _srt_ok(srt)

    cmd: list[str] = [str(get_settings().ffmpeg_bin), "-y", "-i", str(video_in), "-i", str(dub_wav)]
    if srt is not None:
        cmd += ["-i", str(srt)]

    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    if srt is not None:
        cmd += [
            "-map",
            "2:s:0",
            "-c:s",
            "srt",
            "-disposition:s:0",
            "default",
            "-metadata:s:s:0",
            "language=eng",
        ]

    cmd += [
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-metadata:s:a:0",
        "language=eng",
        "-avoid_negative_ts",
        "make_zero",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    logger.info("[v2] export mkv → %s", out_path)
    return out_path


def export_mp4(
    video_in: Path, dub_wav: Path, srt: Path | None, out_path: Path, *, fragmented: bool = False
) -> Path:
    """
    Browser-safe MP4:
      - video re-encoded to H.264 (libx264), yuv420p
      - audio AAC
      - subtitles as mov_text when present
      - when fragmented=True: fast seeks / streaming-friendly moofs
    """
    video_in = Path(video_in)
    dub_wav = Path(dub_wav)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    srt = _srt_ok(srt)

    cmd: list[str] = [str(get_settings().ffmpeg_bin), "-y", "-i", str(video_in), "-i", str(dub_wav)]
    if srt is not None:
        cmd += ["-i", str(srt)]

    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    if srt is not None:
        cmd += ["-map", "2:s:0"]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-metadata:s:a:0",
        "language=eng",
    ]

    if srt is not None:
        cmd += [
            "-c:s",
            "mov_text",
            "-disposition:s:0",
            "default",
            "-metadata:s:s:0",
            "language=eng",
        ]

    movflags = ["+faststart"]
    if fragmented:
        movflags.append("+frag_keyframe")
        movflags.append("+separate_moof")
    cmd += ["-movflags", "".join(movflags)]
    cmd += ["-avoid_negative_ts", "make_zero", str(out_path)]
    subprocess.run(cmd, check=True)
    logger.info("[v2] export mp4%s → %s", " (fragmented)" if fragmented else "", out_path)
    return out_path


def export_hls(video_in: Path, dub_wav: Path, srt: Path | None, out_dir: Path) -> Path:
    """
    Simple HLS VOD export:
      - single 480p variant
      - hls_time 4
      - master playlist at out_dir/master.m3u8

    Note: subtitles are not muxed into HLS here (kept as external SRT beside playlists).
    """
    video_in = Path(video_in)
    dub_wav = Path(dub_wav)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    srt = _srt_ok(srt)

    variant = out_dir / "stream.m3u8"
    seg_pat = out_dir / "seg_%03d.ts"

    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_in),
        "-i",
        str(dub_wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "scale=-2:480",
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-level",
        "3.0",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-hls_time",
        "4",
        "-hls_playlist_type",
        "vod",
        "-hls_segment_filename",
        str(seg_pat),
        str(variant),
    ]
    subprocess.run(cmd, check=True)

    master = out_dir / "master.m3u8"
    master.write_text(
        "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                '#EXT-X-STREAM-INF:BANDWIDTH=1600000,RESOLUTION=854x480,CODECS="avc1.42c01e,mp4a.40.2"',
                "stream.m3u8",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if srt is not None:
        # Keep SRT alongside; player integration is app-specific.
        logger.info("[v2] export hls: subtitle kept as %s", srt)
    logger.info("[v2] export hls → %s", master)
    return master
