from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from anime_v2.plugins.lipsync.preview import preview_lipsync_ranges, write_preview_report
from anime_v2.utils.ffmpeg_safe import run_ffmpeg


def _tmp_dir() -> Path:
    return Path("/workspace/_tmp_lipsync_preview").resolve()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _make_dummy_video(path: Path) -> None:
    """
    Make a tiny MP4 without real faces (testsrc).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=25",
        "-t",
        "2.0",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        str(path),
    ]
    run_ffmpeg(argv, timeout_s=120, retries=0, capture=True)


def main() -> int:
    root = _tmp_dir()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    vid = root / "dummy.mp4"
    _make_dummy_video(vid)

    rep = preview_lipsync_ranges(video=vid, work_dir=root / "work", sample_every_s=0.5, max_frames=20)
    out = write_preview_report(rep, out_path=root / "lipsync_preview.json")
    _assert(out.exists(), "preview report should be written")

    data = json.loads(out.read_text(encoding="utf-8"))
    _assert("recommended_ranges" in data and "warnings" in data, "report schema must include ranges + warnings")
    _assert(isinstance(data["warnings"], list), "warnings must be a list")

    print("verify_lipsync_preview: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as ex:
        print(f"verify_lipsync_preview: FAIL: {ex}", file=sys.stderr)
        raise SystemExit(2)

