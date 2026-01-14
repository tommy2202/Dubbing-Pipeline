#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], *, timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        return p.returncode, (out + ("\n" + err if err else "")).strip()
    except Exception as ex:
        return 1, str(ex)


def _print_kv(k: str, v: str) -> None:
    print(f"- {k}: {v}")


def _tail_text(path: Path, n: int = 200) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(data[-n:])


def main() -> int:
    print("collect_diagnostics (safe)")
    _print_kv("date", _run(["date"], timeout=3)[1] or "unknown")
    _print_kv("platform", platform.platform())
    _print_kv("python", sys.version.splitlines()[0])
    _print_kv("executable", sys.executable)

    # Tooling versions (no secrets).
    for tool in ["ffmpeg", "ffprobe", "git", "docker"]:
        if not shutil.which(tool):
            _print_kv(tool, "not found")
            continue
        code, out = _run([tool, "-version"], timeout=10) if tool in {"ffmpeg", "ffprobe"} else _run([tool, "--version"], timeout=10)
        _print_kv(tool, out.splitlines()[0] if out else f"failed (exit={code})")

    # GPU / CUDA visibility.
    if shutil.which("nvidia-smi"):
        code, out = _run(["nvidia-smi"], timeout=10)
        _print_kv("nvidia-smi", "ok" if code == 0 else f"failed (exit={code})")
        if out:
            print(out.splitlines()[0])
    else:
        _print_kv("nvidia-smi", "not found")

    try:
        import torch  # type: ignore

        _print_kv("torch", getattr(torch, "__version__", "unknown"))
        _print_kv("torch.cuda.is_available", str(bool(torch.cuda.is_available())))
        _print_kv("torch.version.cuda", str(getattr(getattr(torch, "version", None), "cuda", None)))
    except Exception as ex:
        _print_kv("torch", f"not available ({ex})")

    # Safe effective config report (redacted).
    try:
        os.environ.setdefault("STRICT_SECRETS", "0")
        from config.settings import get_safe_config_report  # type: ignore

        rep = get_safe_config_report()
        print("\n== safe_config_report ==")
        # Keep it compact and safe (this report is already redacted).
        keys = [
            "app_root",
            "output_dir",
            "log_dir",
            "state_dir",
            "redis_url_configured",
            "queue_mode",
            "offline_mode",
            "allow_egress",
            "allow_hf_egress",
        ]
        for k in keys:
            if k in rep:
                print(f"- {k}: {rep.get(k)}")
    except Exception as ex:
        print(f"\nWARN: safe_config_report unavailable: {ex}")

    # Log hints (safe tail; assumes secret masking in logs).
    logs_dir = Path(os.environ.get("DUBBING_LOG_DIR", "") or "logs").resolve()
    if logs_dir.exists():
        print("\n== logs (tail) ==")
        # Prefer the newest few log files.
        files = sorted([p for p in logs_dir.glob("*.log") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:3]:
            print(f"\n--- {p.name} (last 80 lines) ---")
            print(_tail_text(p, n=80))
    else:
        print("\n== logs ==")
        print(f"- log_dir not found: {logs_dir}")

    print("\ncollect_diagnostics: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

