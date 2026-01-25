from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ensure_tiny_mp4(
    path: Path,
    *,
    duration_s: float = 1.0,
    skip_message: str | None = None,
) -> Path:
    if shutil.which("ffmpeg") is None:
        if skip_message:
            import pytest

            pytest.skip(skip_message)
        raise RuntimeError("ffmpeg not available")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            f"{float(duration_s):.2f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return path
