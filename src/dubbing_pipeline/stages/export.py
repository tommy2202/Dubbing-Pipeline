from __future__ import annotations

from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.ffmpeg_safe import run_ffmpeg
from dubbing_pipeline.utils.log import logger


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
    run_ffmpeg(cmd, timeout_s=600, retries=0, capture=True)
    logger.info("[dp] export mkv → %s", out_path)
    return out_path


def export_mkv_multitrack(
    *,
    video_in: Path,
    tracks: list[dict[str, str]],
    srt: Path | None,
    out_path: Path,
) -> Path:
    """
    MKV export with multiple audio tracks.

    Requirements:
      - video copied (no re-encode)
      - each audio WAV encoded to AAC
      - per-track metadata: title + language
      - optional SRT soft subs

    tracks: list of:
      {"path": "...wav", "title": "Dubbed (EN)", "language": "eng", "default": "1|0"}
    """
    video_in = Path(video_in)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    srt = _srt_ok(srt)

    cmd: list[str] = [str(get_settings().ffmpeg_bin), "-y", "-i", str(video_in)]
    for t in tracks:
        cmd += ["-i", str(t["path"])]
    if srt is not None:
        cmd += ["-i", str(srt)]

    # map video
    cmd += ["-map", "0:v:0"]
    # map audio inputs 1..N
    for i in range(len(tracks)):
        cmd += ["-map", f"{i+1}:a:0"]
    if srt is not None:
        cmd += [
            "-map",
            f"{len(tracks)+1}:s:0",
            "-c:s",
            "srt",
            "-disposition:s:0",
            "default",
            "-metadata:s:s:0",
            "language=eng",
        ]

    cmd += ["-c:v", "copy"]
    # Encode all audio tracks
    for i, t in enumerate(tracks):
        cmd += [
            f"-c:a:{i}",
            "aac",
            f"-b:a:{i}",
            "192k",
            f"-metadata:s:a:{i}",
            f"language={t.get('language','und')}",
            f"-metadata:s:a:{i}",
            f"title={t.get('title','Audio')}",
        ]
        if str(t.get("default", "0")) in {"1", "true", "yes"}:
            cmd += [f"-disposition:a:{i}", "default"]
        else:
            cmd += [f"-disposition:a:{i}", "0"]

    cmd += ["-avoid_negative_ts", "make_zero", str(out_path)]
    run_ffmpeg(cmd, timeout_s=900, retries=0, capture=True)
    logger.info("[dp] export multitrack mkv → %s", out_path)
    return out_path


def export_m4a(
    audio_in: Path, out_path: Path, *, title: str | None = None, language: str | None = None
) -> Path:
    """
    Sidecar audio export for MP4 fallback (players vary in multi-audio support).
    """
    audio_in = Path(audio_in)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        str(get_settings().ffmpeg_bin),
        "-y",
        "-i",
        str(audio_in),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
    ]
    if language:
        cmd += ["-metadata:s:a:0", f"language={language}"]
    if title:
        cmd += ["-metadata:s:a:0", f"title={title}"]
    cmd += [str(out_path)]
    run_ffmpeg(cmd, timeout_s=300, retries=0, capture=True)
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
    run_ffmpeg(cmd, timeout_s=900, retries=0, capture=True)
    logger.info("[dp] export mp4%s → %s", " (fragmented)" if fragmented else "", out_path)
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
        str(get_settings().ffmpeg_bin),
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
    run_ffmpeg(cmd, timeout_s=900, retries=0, capture=True)

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
        logger.info("[dp] export hls: subtitle kept as %s", srt)
    logger.info("[dp] export hls → %s", master)
    return master


def export_mobile_mp4(
    *,
    video_in: Path,
    audio_wav: Path | None,
    out_path: Path,
    crf: int = 22,
    audio_bitrate: str = "128k",
) -> Path:
    """
    Mobile-friendly MP4 for iOS/Android:
      - H.264 baseline + yuv420p (max compatibility)
      - AAC-LC
      - +faststart for progressive playback

    If audio_wav is None, uses the source video's first audio track.
    """
    video_in = Path(video_in)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [str(get_settings().ffmpeg_bin), "-y", "-i", str(video_in)]
    if audio_wav is not None:
        cmd += ["-i", str(audio_wav)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        # Original audio
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    cmd += [
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-level",
        "3.1",
        "-preset",
        "veryfast",
        "-crf",
        str(int(crf)),
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate),
        "-movflags",
        "+faststart",
        "-sn",
        "-avoid_negative_ts",
        "make_zero",
        str(out_path),
    ]
    run_ffmpeg(cmd, timeout_s=1200, retries=0, capture=True)
    return out_path


def export_mobile_hls(
    *,
    video_in: Path,
    dub_wav: Path,
    out_dir: Path,
) -> Path:
    """
    Mobile HLS VOD export for maximum iOS reliability.

    Writes:
      - out_dir/master.m3u8 (existing export_hls behavior)
      - out_dir/index.m3u8 (copy of master for mobile-friendly naming)
    """
    out_dir = Path(out_dir)
    master = export_hls(video_in=Path(video_in), dub_wav=Path(dub_wav), srt=None, out_dir=out_dir)
    try:
        idx = out_dir / "index.m3u8"
        if master.exists():
            idx.write_text(master.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    except Exception:
        pass
    return master
