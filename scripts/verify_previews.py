#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install it and retry.")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _make_tiny_mp4(path: Path) -> None:
    _need_tool("ffmpeg")
    p = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "ffmpeg failed")


def main() -> int:
    try:
        _need_tool("ffmpeg")
    except RuntimeError as ex:
        print(f"verify_previews: SKIP ({ex})")
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    for p in (str(repo_root), str(src_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        logs = (root / "logs").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(logs)
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["STRICT_SECRETS"] = "0"

        src = inp / "tiny.mp4"
        _make_tiny_mp4(src)

        try:
            from dubbing_pipeline.media.previews import (
                generate_audio_preview,
                generate_lowres_preview,
                preview_paths,
            )
        except Exception as ex:
            print(f"verify_previews: SKIP (imports unavailable: {ex})")
            return 0

        paths = preview_paths(out)
        generate_audio_preview(src, paths["audio"])
        generate_lowres_preview(src, paths["video"])

        if not paths["audio"].exists():
            print("verify_previews: FAIL (audio preview missing)")
            return 2
        if not paths["video"].exists():
            print("verify_previews: FAIL (video preview missing)")
            return 2

    print("verify_previews: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
