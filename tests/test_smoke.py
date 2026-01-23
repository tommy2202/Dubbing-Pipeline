from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


@pytest.mark.smoke
@pytest.mark.optional_deps
def test_smoke_whisper_small_and_tts_one_line(tmp_path: Path) -> None:
    """
    Minimal ops smoke:
    - synthesize 1 line on CPU (espeak-ng)
    - load Whisper "small" and transcribe the generated audio
    """
    if not _have("espeak-ng"):
        pytest.skip("espeak-ng not available")
    if not _have("ffmpeg"):
        pytest.skip("ffmpeg not available (whisper audio decode)")

    try:
        import whisper  # type: ignore
    except Exception:
        pytest.skip("openai-whisper not installed")

    # Ensure we don't run in offline mode in CI.
    os.environ.pop("OFFLINE_MODE", None)
    os.environ.setdefault("ALLOW_EGRESS", "1")

    wav = tmp_path / "hello.wav"
    subprocess.run(["espeak-ng", "-w", str(wav), "hello world"], check=True)
    assert wav.exists() and wav.stat().st_size > 0

    model = whisper.load_model("small", device="cpu")
    result = model.transcribe(str(wav), fp16=False, language="en")
    text = str(result.get("text") or "").lower()
    assert "hello" in text
