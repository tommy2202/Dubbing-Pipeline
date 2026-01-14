#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cmd: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install it and retry.")


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
    _need_tool("ffmpeg")
    _need_tool("ffprobe")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        inp = (root / "Input").resolve()
        out = (root / "Output").resolve()
        inp.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)

        mp4 = inp / "tiny.mp4"
        _make_tiny_mp4(mp4)

        # Set process env for config sanity checks (avoid unwritable container defaults like /models).
        os.environ["APP_ROOT"] = str(root)
        os.environ["INPUT_DIR"] = str(inp)
        os.environ["DUBBING_OUTPUT_DIR"] = str(out)
        os.environ["DUBBING_LOG_DIR"] = str(out / "logs")
        os.environ["MODELS_DIR"] = str((root / "models").resolve())
        os.environ.setdefault("COOKIE_SECURE", "0")
        os.environ.setdefault("STRICT_SECRETS", "0")

        # CLI should be runnable via module invocation (works even if console scripts aren't on PATH).
        help_p = _run([sys.executable, "-m", "dubbing_pipeline.cli", "--help"], timeout=30)
        if help_p.returncode != 0:
            print("FAIL: CLI help failed", file=sys.stderr)
            print(help_p.stderr or help_p.stdout, file=sys.stderr)
            return 2

        # Lite pipeline smoke: dry-run only (no model downloads required).
        # Also prints safe config (no secrets) so operators have context in logs.
        env = dict(os.environ)
        env["APP_ROOT"] = os.environ["APP_ROOT"]
        env["INPUT_DIR"] = os.environ["INPUT_DIR"]
        env["DUBBING_OUTPUT_DIR"] = os.environ["DUBBING_OUTPUT_DIR"]
        env["DUBBING_LOG_DIR"] = os.environ["DUBBING_LOG_DIR"]
        env["MODELS_DIR"] = os.environ["MODELS_DIR"]
        env["COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0")
        env["STRICT_SECRETS"] = os.environ.get("STRICT_SECRETS", "0")

        dry = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-m",
                "dubbing_pipeline.cli",
                str(mp4),
                "--mode",
                "low",
                "--device",
                "cpu",
                "--tgt-lang",
                "en",
                "--print-config",
                "--dry-run",
            ],
            timeout=120,
            env=env,
            text=True,
            capture_output=True,
        )
        if dry.returncode != 0:
            print("FAIL: dry-run CLI smoke failed", file=sys.stderr)
            print(dry.stderr or dry.stdout, file=sys.stderr)
            return 2

        # Model/cache sanity (lite): verify directories are writable.
        try:
            from dubbing_pipeline.config import get_settings

            get_settings.cache_clear()
            s = get_settings()
            dirs = [
                ("output_dir", Path(s.output_dir)),
                ("log_dir", Path(s.log_dir)),
                ("models_dir", Path(s.models_dir)),
            ]
            for name, p in dirs:
                p = p.expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                print(f"{name}: writable ({p})")
        except Exception as ex:
            print(
                "FAIL: model/cache directory sanity check failed.\n"
                f"Error: {ex}\n"
                "Fix: ensure Docker volumes/paths are writable by the app user.",
                file=sys.stderr,
            )
            return 2

    print("e2e_smoke_cli: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

