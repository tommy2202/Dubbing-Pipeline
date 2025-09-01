import pathlib, subprocess, json
from anime_v1.utils import logger, checkpoints

def _write_srt(transcript_json: pathlib.Path, srt_path: pathlib.Path):
    data = json.loads(transcript_json.read_text())
    segments = data.get("segments", [])
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = _format_ts(seg["start"])
            end   = _format_ts(seg["end"])
            text  = seg["text"].strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

def _format_ts(sec: float):
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec))*1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def run(video: pathlib.Path, ckpt_dir: pathlib.Path, **_):
    dubbed = ckpt_dir / "dubbed.wav"
    transcript = ckpt_dir / "transcript.json"
    if not dubbed.exists():
        logger.info("No dubbed.wav – export skipped.")
        return
    out_dir = pathlib.Path("/data/out")
    out_dir.mkdir(parents=True, exist_ok=True)
    mkv_path = out_dir / f"{video.stem}_dubbed.mkv"
    srt_path = out_dir / f"{video.stem}.srt"
    _write_srt(transcript, srt_path)
    logger.info("Muxing soft‑sub MKV → %s", mkv_path)
    cmd = [
        "ffmpeg","-y",
        "-i", str(video),
        "-i", str(dubbed),
        "-map","0:v:0","-map","1:a:0",
        "-c:v","copy","-c:a","aac",
        "-metadata:s:a:0","language=eng",
        str(mkv_path)
    ]
    subprocess.run(cmd, check=True)
    logger.info("Export done.")
    return mkv_path
