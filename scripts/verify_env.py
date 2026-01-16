#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Any


@dataclass(frozen=True, slots=True)
class DepCheck:
    name: str
    required: bool
    import_name: str | None = None


def _version(dist_name: str) -> str | None:
    try:
        return metadata.version(dist_name)
    except Exception:
        return None


def _can_import(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _run_version(cmd: list[str]) -> str | None:
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    # ffmpeg prints version to stdout; some builds use stderr. Join both.
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    return out.splitlines()[0] if out else (err.splitlines()[0] if err else None)


def main() -> int:
    print("verify_env")
    print(f"- python: {sys.version.splitlines()[0]}")
    print(f"- platform: {platform.platform()}")
    print(f"- executable: {sys.executable}")

    # Required external tools
    ffmpeg_v = _run_version(["ffmpeg", "-version"])
    ffprobe_v = _run_version(["ffprobe", "-version"])
    if not ffmpeg_v or not ffprobe_v:
        print("- ffmpeg: MISSING (required)", file=sys.stderr)
        print("- ffprobe: MISSING (required)", file=sys.stderr)
        return 2
    print(f"- ffmpeg: {ffmpeg_v}")
    print(f"- ffprobe: {ffprobe_v}")

    checks: list[DepCheck] = [
        DepCheck("click", True, "click"),
        DepCheck("fastapi", True, "fastapi"),
        DepCheck("uvicorn", True, "uvicorn"),
        DepCheck("pydantic", True, "pydantic"),
        DepCheck("pydantic-settings", True, "pydantic_settings"),
        DepCheck("structlog", True, "structlog"),
        # common runtime deps (soft-required but present in pyproject)
        DepCheck("sqlitedict", True, "sqlitedict"),
        DepCheck("jinja2", True, "jinja2"),
        DepCheck("python-multipart", True, "multipart"),
        # optional ML / audio
        DepCheck("openai-whisper", False, "whisper"),
        DepCheck("torch", False, "torch"),
        DepCheck("TTS", False, "TTS"),
        DepCheck("demucs", False, "demucs"),
        DepCheck("librosa", False, "librosa"),
        DepCheck("aeneas", False, "aeneas"),
        DepCheck("pyannote.audio", False, "pyannote.audio"),
        DepCheck("resemblyzer", False, "resemblyzer"),
        DepCheck("speechbrain", False, "speechbrain"),
        DepCheck("sklearn", False, "sklearn"),
        DepCheck("webrtcvad", False, "webrtcvad"),
        DepCheck("aiortc", False, "aiortc"),
        DepCheck("av", False, "av"),
        # legacy extras
        DepCheck("pydub", False, "pydub"),
        DepCheck("gradio", False, "gradio"),
        DepCheck("vosk", False, "vosk"),
    ]

    missing_required: list[str] = []
    present_optional: list[str] = []
    missing_optional: list[str] = []

    print("- deps:")
    for c in checks:
        dist_v = _version(c.name) if c.import_name is None else None
        ok = _can_import(c.import_name) if c.import_name else (dist_v is not None)
        if ok:
            v = dist_v or _version(c.name) or "unknown"
            print(f"  - {c.name}: OK ({v})")
            if not c.required:
                present_optional.append(c.name)
        else:
            tag = "MISSING (required)" if c.required else "missing (optional)"
            print(f"  - {c.name}: {tag}")
            if c.required:
                missing_required.append(c.name)
            else:
                missing_optional.append(c.name)

    # GPU availability (best-effort)
    gpu = False
    try:
        import torch  # type: ignore

        gpu = bool(torch.cuda.is_available())
    except Exception:
        gpu = False
    print(f"- gpu_available: {gpu}")

    # Feature availability report (heuristic)
    features: dict[str, Any] = {
        "asr_whisper": _can_import("whisper"),
        "tts_coqui": _can_import("TTS"),
        "mixing_demucs": _can_import("demucs"),
        "align_aeneas": _can_import("aeneas"),
        "music_detect_librosa": _can_import("librosa"),
        "diarization_pyannote": _can_import("pyannote.audio"),
        "diarization_resemblyzer": _can_import("resemblyzer"),
        "diarization_speechbrain": _can_import("speechbrain"),
        "webrtc": _can_import("aiortc") and _can_import("av"),
    }
    print("- features_enabled:")
    for k in sorted(features.keys()):
        print(f"  - {k}: {bool(features[k])}")

    # Config availability (safe report)
    try:
        # Ensure we don't enforce secrets by default in this verifier.
        os.environ.setdefault("STRICT_SECRETS", "0")
        from config.settings import get_safe_config_report  # type: ignore

        rep = get_safe_config_report()
        print("- safe_config_report: OK")
        # Keep output compact for CI logs
        print(f"  - strict_secrets: {bool(rep.get('strict_secrets'))}")
    except Exception as ex:
        print(f"- safe_config_report: FAILED ({ex})", file=sys.stderr)
        # still allow env verification to proceed (config can depend on secrets in strict mode)

    if missing_required:
        print(f"FAIL: missing required deps: {', '.join(missing_required)}", file=sys.stderr)
        return 2

    if missing_optional:
        print(f"WARN: missing optional deps: {', '.join(missing_optional)}", file=sys.stderr)

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

