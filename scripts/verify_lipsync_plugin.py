from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


def _write_fake_wav2lip_repo(repo_dir: Path) -> None:
    """
    Creates a minimal "Wav2Lip-like" repo with infer.py that just copies input video to outfile.
    This exercises our subprocess + ffmpeg mux path without requiring real Wav2Lip deps.
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    infer = repo_dir / "infer.py"
    infer.write_text(
        "\n".join(
            [
                "import argparse",
                "import shutil",
                "from pathlib import Path",
                "",
                "ap = argparse.ArgumentParser()",
                "ap.add_argument('--checkpoint_path')",
                "ap.add_argument('--face')",
                "ap.add_argument('--audio')",
                "ap.add_argument('--outfile')",
                "ap.add_argument('--device', default=None)",
                "ap.add_argument('--box', nargs=4, default=None)",
                "args = ap.parse_args()",
                "out = Path(args.outfile)",
                "out.parent.mkdir(parents=True, exist_ok=True)",
                "# Fake inference: copy the face video through.",
                "shutil.copyfile(args.face, out)",
            ]
        ),
        encoding="utf-8",
    )
    (repo_dir / "__init__.py").write_text("", encoding="utf-8")


def _write_dummy_checkpoint(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dummy")


def main() -> int:
    try:
        from dubbing_pipeline.plugins.lipsync.base import LipSyncRequest
        from dubbing_pipeline.plugins.lipsync.wav2lip_plugin import Wav2LipPlugin
    except Exception as ex:
        print(f"IMPORT_FAILED: {ex}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="verify_lipsync_plugin_"))
    try:
        repo = tmp / "third_party" / "wav2lip"
        ckpt = tmp / "models" / "wav2lip" / "wav2lip.pth"
        _write_fake_wav2lip_repo(repo)
        _write_dummy_checkpoint(ckpt)

        # Generate tiny sample media with ffmpeg if available; else dry-run only.
        video = tmp / "in.mp4"
        audio = tmp / "dub.wav"
        out = tmp / "out.mp4"
        work = tmp / "work"

        import subprocess

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            p = Wav2LipPlugin(wav2lip_dir=repo, checkpoint_path=ckpt)
            ok = p.is_available()
            print(json.dumps({"wav2lip_available": ok, "ffmpeg_on_path": bool(ffmpeg)}, sort_keys=True))
            return 0 if ok else 1

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=1",
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
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(audio),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        plugin = Wav2LipPlugin(wav2lip_dir=repo, checkpoint_path=ckpt)
        if not plugin.is_available():
            print("WAV2LIP_DETECT_FAILED")
            return 1

        req = LipSyncRequest(
            input_video=video,
            dubbed_audio_wav=audio,
            output_video=out,
            work_dir=work,
            face_mode="center",
            device="cpu",
            timeout_s=120,
        )
        plugin.run(req)
        if not out.exists() or out.stat().st_size == 0:
            print("OUTPUT_MISSING")
            return 1

        print("VERIFY_LIPSYNC_PLUGIN_OK")
        return 0
    except Exception as ex:
        print(f"VERIFY_LIPSYNC_PLUGIN_FAILED: {ex}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

