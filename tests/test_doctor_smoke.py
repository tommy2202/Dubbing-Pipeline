from __future__ import annotations

import json
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.doctor.wizard import run_doctor


def test_doctor_smoke(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path.resolve()
    monkeypatch.setenv("APP_ROOT", str(root))
    monkeypatch.setenv("INPUT_DIR", str(root / "Input"))
    monkeypatch.setenv("DUBBING_OUTPUT_DIR", str(root / "Output"))
    monkeypatch.setenv("DUBBING_LOG_DIR", str(root / "logs"))
    monkeypatch.setenv("DUBBING_STATE_DIR", str(root / "_state"))
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("STRICT_SECRETS", "0")
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setenv("ALLOW_EGRESS", "0")
    monkeypatch.setenv("ALLOW_HF_EGRESS", "0")
    monkeypatch.setenv("ENABLE_MODEL_DOWNLOADS", "0")
    monkeypatch.setenv("DUBBING_DOCTOR_MODE", "low")
    monkeypatch.setenv("TTS_PROVIDER", "espeak")
    monkeypatch.setenv("VOICE_MODE", "single")
    monkeypatch.setenv("DIARIZER", "off")
    monkeypatch.setenv("SEPARATION", "off")
    monkeypatch.setenv("LIPSYNC", "off")
    monkeypatch.setenv("QUEUE_MODE", "fallback")
    monkeypatch.setenv("DOCTOR_SMOKE_TIMEOUT", "10")
    get_settings.cache_clear()

    report, txt_path, json_path = run_doctor(report_dir=root / "reports", require_gpu=False)
    assert txt_path.exists()
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert "metadata" in payload
    assert "summary" in payload
    assert isinstance(payload.get("checks"), list)
    assert report.summary() == payload.get("summary")
