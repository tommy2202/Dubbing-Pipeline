from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from dubbing_pipeline.streaming.runner import run_streaming
    except Exception as ex:
        print(f"IMPORT_FAILED: {ex}")
        return 2

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("SKIP: ffmpeg not found on PATH")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="verify_streaming_mode_"))
    try:
        video = tmp / "in.mp4"
        # 3s silent video (black)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=3",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        out_dir = tmp / "Output" / "JOB"
        res = run_streaming(
            video=video,
            out_dir=out_dir,
            device="cpu",
            asr_model="small",
            src_lang="auto",
            tgt_lang="en",
            mt_engine="auto",
            mt_lowconf_thresh=-0.45,
            glossary=None,
            style=None,
            stream=True,
            chunk_seconds=2.0,
            overlap_seconds=1.0,
            stream_output="final",
            stream_concurrency=1,
            timing_fit=False,
            pacing=False,
            align_mode="stretch",
            emotion_mode="off",
            expressive="off",
            dry_run=True,
        )

        man = Path(str(res.get("manifest")))
        if not man.exists():
            print("MANIFEST_MISSING")
            return 1
        data = json.loads(man.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "chunks" not in data:
            print("MANIFEST_INVALID")
            return 1
        chunks = data.get("chunks", [])
        if not isinstance(chunks, list) or not chunks:
            print("NO_CHUNKS")
            return 1
        # ensure at least one chunk mp4 exists
        ok_any = False
        for c in chunks:
            p = c.get("chunk_mp4") if isinstance(c, dict) else None
            if p and Path(str(p)).exists():
                ok_any = True
                break
        if not ok_any:
            print("CHUNK_MP4_MISSING")
            return 1

        final = res.get("final")
        if not final or not Path(str(final)).exists():
            print("FINAL_MISSING")
            return 1

        print("VERIFY_STREAMING_MODE_OK")
        return 0
    except Exception as ex:
        print(f"VERIFY_STREAMING_MODE_FAILED: {ex}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

