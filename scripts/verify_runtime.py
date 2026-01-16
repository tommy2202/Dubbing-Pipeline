#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


def main() -> int:
    # Keep runtime verification lightweight/offline-first by default.
    os.environ.setdefault("STRICT_SECRETS", "0")
    os.environ.setdefault("OFFLINE_MODE", "1")
    os.environ.setdefault("ALLOW_EGRESS", "0")
    os.environ.setdefault("ALLOW_HF_EGRESS", "0")

    from config.settings import get_safe_config_report, get_settings

    s = get_settings()

    # 1) Safe config report
    report = get_safe_config_report()
    print("SAFE_CONFIG_REPORT:")
    print(json.dumps(report, indent=2, sort_keys=True))

    # 2) ffmpeg/ffprobe availability
    try:
        from dubbing_pipeline.utils.ffmpeg_safe import ffprobe_duration_seconds

        # Validate tool availability (accept either absolute paths or PATH-resolvable names).
        ffmpeg_bin_s = str(s.ffmpeg_bin)
        ffprobe_bin_s = str(s.ffprobe_bin)
        if not Path(ffmpeg_bin_s).exists() and not shutil.which(ffmpeg_bin_s):
            return _fail(f"ffmpeg_bin not found or not on PATH: {ffmpeg_bin_s}")
        if not Path(ffprobe_bin_s).exists() and not shutil.which(ffprobe_bin_s):
            return _fail(f"ffprobe_bin not found or not on PATH: {ffprobe_bin_s}")

        sample = REPO_ROOT / "samples" / "sample.mp4"
        if sample.exists():
            _ = ffprobe_duration_seconds(sample, timeout_s=10)
    except Exception as ex:
        return _fail(f"ffmpeg/ffprobe check failed: {ex}")

    # 3) Output/log dirs writable
    for p in (Path(s.output_dir), Path(s.log_dir)):
        try:
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".verify_runtime_write_test"
            t.write_text("ok", encoding="utf-8")
            t.unlink(missing_ok=True)
        except Exception as ex:
            return _fail(f"Path not writable: {p} ({ex})")

    print("VERIFY_RUNTIME_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
