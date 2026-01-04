import json
import pathlib
import subprocess

from config.settings import get_settings

from anime_v1.utils import logger


def _write_srt(transcript_json: pathlib.Path, srt_path: pathlib.Path):
    data = json.loads(transcript_json.read_text())
    segments = data.get("segments", [])
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = _format_ts(seg["start"])
            end = _format_ts(seg["end"])
            text = seg["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def _format_ts(sec: float):
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def run(
    video: pathlib.Path,
    ckpt_dir: pathlib.Path,
    *,
    out_dir: pathlib.Path | None = None,
    keep_bg: bool = True,
    transcript_override: pathlib.Path | None = None,
    **_,
):
    dubbed = ckpt_dir / "dubbed.wav"
    transcript = transcript_override or (ckpt_dir / "transcript.json")
    if not dubbed.exists():
        logger.info("No dubbed.wav – export skipped.")
        return
    out_dir = out_dir or pathlib.Path(str(get_settings().v1_output_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    mkv_path = out_dir / f"{video.stem}_dubbed.mkv"
    srt_path = out_dir / f"{video.stem}.srt"
    _write_srt(transcript, srt_path)
    logger.info("Muxing → %s (keep_bg=%s)", mkv_path, keep_bg)

    if keep_bg:
        bg = ckpt_dir / "background.wav"
        if bg.exists():
            logger.info("Using separated background for mixing → %s", bg)
            # Mix separated background with dubbed voice (no original voice bleed)
            cmd = [
                str(get_settings().public.ffmpeg_bin),
                "-y",
                "-i",
                str(video),
                "-i",
                str(bg),
                "-i",
                str(dubbed),
                "-filter_complex",
                "[1:a]volume=1.0[bg];[2:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                str(mkv_path),
            ]
        else:
            # Mix original audio at low volume under dubbed voice
            cmd = [
                str(get_settings().public.ffmpeg_bin),
                "-y",
                "-i",
                str(video),
                "-i",
                str(dubbed),
                "-filter_complex",
                "[0:a]volume=0.25[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=eng",
                str(mkv_path),
            ]
    else:
        # Replace audio with dubbed track only
        cmd = [
            str(get_settings().public.ffmpeg_bin),
            "-y",
            "-i",
            str(video),
            "-i",
            str(dubbed),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=eng",
            str(mkv_path),
        ]

    subprocess.run(cmd, check=True)
    logger.info("Export done.")
    return mkv_path
