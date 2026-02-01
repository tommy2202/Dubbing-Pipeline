from __future__ import annotations

import importlib.util
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from dubbing_pipeline.api.remote_access import resolve_access_posture
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.doctor.container import (
    check_ffmpeg,
    check_full_job,
    check_import_smoke,
    check_ntfy,
    check_required_secrets,
    check_redis,
    check_torch_cuda,
    check_turn,
    check_writable_dirs,
)
from dubbing_pipeline.doctor.host import (
    check_docker_installed,
    check_nvidia_container_toolkit,
    check_tailscale_installed,
)
from dubbing_pipeline.doctor.models import build_model_requirement_checks, selected_pipeline_mode
from dubbing_pipeline.plugins.lipsync.wav2lip_plugin import Wav2LipPlugin
from dubbing_pipeline.modes import HardwareCaps, resolve_effective_settings
from dubbing_pipeline.utils.doctor_types import CheckResult, DoctorReport
from dubbing_pipeline.utils.doctor_report import format_report_json, format_report_text
from dubbing_pipeline.utils.doctor_runner import run_checks, write_report


def default_report_paths(*, report_dir: Path | None = None) -> tuple[Path, Path]:
    s = get_settings()
    base = Path(report_dir) if report_dir else Path(getattr(s, "output_dir", Path.cwd())).resolve()
    reports_dir = (base / "reports").resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)
    txt_path = reports_dir / "doctor_report.txt"
    json_path = reports_dir / "doctor_report.json"
    return txt_path, json_path


def _decorate(
    res: CheckResult,
    *,
    stage: str,
    required: bool | None = None,
    category: str | None = None,
) -> CheckResult:
    details = res.details
    if not isinstance(details, dict):
        details = {"detail": details}
    details = dict(details)
    details.setdefault("stage", stage)
    if required is not None:
        details.setdefault("required", bool(required))
    if category:
        details.setdefault("category", str(category))
    name = res.name or res.id
    if not name.lower().startswith(f"stage {stage}".lower()):
        name = f"Stage {stage}: {name}"
    return replace(res, name=name, details=details)


def _can_import(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _production_env() -> str:
    for key in ("DUBBING_ENV", "APP_ENV", "ENVIRONMENT", "ENV", "RUN_ENV"):
        raw = os.environ.get(key)
        if raw:
            return str(raw).strip().lower()
    return ""


def check_db_writable() -> CheckResult:
    s = get_settings()
    state_dir = Path(
        str(getattr(s, "state_dir", None) or (Path(s.output_dir) / "_state"))
    ).resolve()
    ok = False
    error = ""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        ok = True
    except Exception as ex:
        ok = False
        error = str(ex)[:200]
    status = "PASS" if ok else "FAIL"
    return CheckResult(
        id="db_writable",
        name="Database writable (state dir)",
        status=status,
        details={
            "state_dir": str(state_dir),
            "ok": bool(ok),
            "error": error,
        },
        remediation=["Ensure DUBBING_STATE_DIR is writable."],
    )


def _feature_import_check(
    *,
    feature_id: str,
    feature_name: str,
    modules: list[str],
    enabled: bool,
    required: bool,
    description: str,
    enable_steps: list[str],
    install_steps: list[str],
) -> CheckResult:
    installed = all(_can_import(m) for m in modules) if modules else False
    status = "PASS" if installed or not enabled else ("FAIL" if required else "WARN")
    return CheckResult(
        id=feature_id,
        name=feature_name,
        status=status,
        details={
            "feature": feature_id,
            "enabled": bool(enabled),
            "required": bool(required),
            "description": description,
            "modules": modules,
            "installed": bool(installed),
            "enable_steps": enable_steps,
            "install_steps": {
                "linux": install_steps,
                "macos": install_steps,
                "windows": install_steps,
            },
        },
        remediation=install_steps if (enabled and not installed) else [],
    )


def build_feature_import_checks() -> list[Callable[[], CheckResult]]:
    s = get_settings()
    mode = selected_pipeline_mode()
    caps = HardwareCaps.detect()
    base = {
        "diarizer": str(getattr(s, "diarizer", "auto") or "auto"),
        "speaker_smoothing": bool(getattr(s, "speaker_smoothing", False)),
        "voice_memory": bool(getattr(s, "voice_memory", False)),
        "voice_mode": str(getattr(s, "voice_mode", "clone") or "clone"),
        "music_detect": bool(getattr(s, "music_detect", False)),
        "separation": str(getattr(s, "separation", "off") or "off"),
        "mix_mode": str(getattr(s, "mix_mode", "legacy") or "legacy"),
        "timing_fit": bool(getattr(s, "timing_fit", False)),
        "pacing": bool(getattr(s, "pacing", False)),
        "qa": False,
        "director": bool(getattr(s, "director", False)),
        "multitrack": bool(getattr(s, "multitrack", False)),
    }
    eff = resolve_effective_settings(mode=mode, base=base, overrides={}, caps=caps)

    checks: list[Callable[[], CheckResult]] = []

    # ASR / Whisper
    checks.append(
        lambda: _feature_import_check(
            feature_id="asr_whisper",
            feature_name="ASR (Whisper) package",
            modules=["whisper"],
            enabled=True,
            required=(mode != "low"),
            description="Automatic speech recognition for transcription.",
            enable_steps=["Set ASR_MODEL or WHISPER_MODEL to choose sizes."],
            install_steps=["python3 -m pip install openai-whisper"],
        )
    )

    # TTS / Coqui
    tts_provider = str(getattr(s, "tts_provider", "auto") or "auto").strip().lower()
    voice_mode = str(eff.voice_mode or "clone").strip().lower()
    tts_enabled = tts_provider in {"auto", "xtts", "basic"}
    tts_required = tts_enabled and mode != "low" and voice_mode in {"clone", "preset"}
    checks.append(
        lambda: _feature_import_check(
            feature_id="tts_coqui",
            feature_name="TTS (Coqui) package",
            modules=["TTS"],
            enabled=bool(tts_enabled),
            required=bool(tts_required),
            description="Neural text-to-speech (XTTS / basic TTS).",
            enable_steps=["Set TTS_PROVIDER=auto or xtts."],
            install_steps=["python3 -m pip install TTS"],
        )
    )

    # Diarization
    diarizer = str(eff.diarizer or "auto").strip().lower()
    enable_pyannote = bool(getattr(s, "enable_pyannote", False))
    diar_enabled = diarizer != "off"
    diar_required = diar_enabled and mode != "low"
    if diarizer == "pyannote" or enable_pyannote:
        checks.append(
            lambda: _feature_import_check(
                feature_id="diarization_pyannote",
                feature_name="Diarization (pyannote.audio)",
                modules=["pyannote.audio"],
                enabled=bool(diar_enabled),
                required=bool(diar_required),
                description="Speaker diarization with pyannote.audio.",
                enable_steps=["Set ENABLE_PYANNOTE=1 and HUGGINGFACE_TOKEN."],
                install_steps=["python3 -m pip install pyannote.audio"],
            )
        )
    else:
        checks.append(
            lambda: _feature_import_check(
                feature_id="diarization_speechbrain",
                feature_name="Diarization (speechbrain)",
                modules=["speechbrain"],
                enabled=bool(diar_enabled),
                required=bool(diar_required),
                description="Speaker diarization with speechbrain.",
                enable_steps=["Set DIARIZER=speechbrain."],
                install_steps=["python3 -m pip install speechbrain"],
            )
        )

    # Demucs separation
    separation = str(eff.separation or "off").strip().lower()
    sep_enabled = separation == "demucs"
    checks.append(
        lambda: _feature_import_check(
            feature_id="demucs_pkg",
            feature_name="Demucs separation",
            modules=["demucs"],
            enabled=bool(sep_enabled),
            required=bool(sep_enabled and mode != "low"),
            description="Music/voice separation for better mixes.",
            enable_steps=["Set SEPARATION=demucs."],
            install_steps=["python3 -m pip install demucs"],
        )
    )

    # Wav2Lip
    lipsync = str(getattr(s, "lipsync", "off") or "off").strip().lower()
    lipsync_enabled = lipsync != "off"

    def _wav2lip_check() -> CheckResult:
        available = bool(Wav2LipPlugin().is_available())
        required = bool(lipsync_enabled and mode == "high")
        status = "PASS" if available or not lipsync_enabled else ("FAIL" if required else "WARN")
        return CheckResult(
            id="wav2lip",
            name="Wav2Lip lipsync",
            status=status,
            details={
                "feature": "wav2lip",
                "enabled": bool(lipsync_enabled),
                "required": bool(required),
                "description": "Video lipsync enhancement (Wav2Lip).",
                "available": bool(available),
                "enable_steps": ["Set LIPSYNC=wav2lip."],
                "install_steps": {
                    "linux": ["python3 scripts/download_models.py"],
                    "macos": ["python3 scripts/download_models.py"],
                    "windows": ["python3 scripts/download_models.py"],
                },
            },
            remediation=["python3 scripts/download_models.py"] if lipsync_enabled and not available else [],
        )

    checks.append(_wav2lip_check)

    return checks


def check_security_secrets() -> CheckResult:
    res = check_required_secrets()
    strict_env = bool(int(os.environ.get("STRICT_SECRETS", "0") or "0"))
    env_name = _production_env()
    prod = env_name in {"prod", "production"}
    if res.status == "FAIL" and not (strict_env or prod):
        res = replace(res, status="WARN")
    details = res.details if isinstance(res.details, dict) else {"detail": res.details}
    details = dict(details)
    details["strict_secrets"] = bool(strict_env)
    details["environment"] = env_name or "unknown"
    return replace(res, details=details)


def check_remote_access_posture() -> CheckResult:
    posture = resolve_access_posture()
    warnings = list(posture.get("warnings") or [])
    mode = str(posture.get("mode") or "off")
    if mode == "cloudflare" and not bool(posture.get("cloudflare_access_configured")):
        warnings.append("Cloudflare Access is not configured (team_domain/aud missing).")
    status = "PASS" if not warnings else "WARN"
    details = {
        "mode": mode,
        "allowed_subnets": posture.get("allowed_subnets") or [],
        "trusted_proxy_subnets": posture.get("trusted_proxy_subnets") or [],
        "trust_proxy_headers": bool(posture.get("trust_proxy_headers")),
        "effective_trust_proxy_headers": bool(posture.get("effective_trust_proxy_headers")),
        "cloudflare_access_configured": bool(posture.get("cloudflare_access_configured")),
        "warnings": warnings,
    }
    return CheckResult(
        id="remote_access_posture",
        name="Remote access posture",
        status=status,
        details=details,
        remediation=["Review ACCESS_MODE/REMOTE_ACCESS_MODE settings and proxy trust config."],
    )


def check_queue_redis() -> CheckResult:
    s = get_settings()
    queue_mode = str(getattr(s, "queue_mode", "auto") or "auto").strip().lower()
    res = check_redis()
    details = res.details if isinstance(res.details, dict) else {"detail": res.details}
    details = dict(details)
    details["queue_mode"] = queue_mode
    if queue_mode == "redis":
        status = "PASS" if res.status == "PASS" else "FAIL"
    else:
        configured = bool(details.get("configured"))
        reachable = bool(details.get("reachable"))
        status = "PASS" if (not configured or reachable) else "WARN"
    return replace(res, status=status, details=details)


def check_high_mode_readiness() -> CheckResult:
    checks = build_model_requirement_checks(mode="high")
    missing: list[str] = []
    for chk in checks:
        res = chk()
        if res.status != "PASS":
            missing.append(res.id)
    caps = HardwareCaps.detect()
    gpu_ok = bool(caps.gpu_available)
    status = "PASS" if (not missing and gpu_ok) else "WARN"
    return CheckResult(
        id="high_mode_readiness",
        name="High mode readiness checklist",
        status=status,
        details={
            "mode": "high",
            "gpu_available": gpu_ok,
            "missing_checks": missing,
        },
        remediation=[
            "Install optional model weights and enable GPU to reach High mode.",
        ]
        if status != "PASS"
        else [],
    )


def build_setup_checks(*, require_gpu: bool, include_smoke: bool = True) -> list[Callable[[], CheckResult]]:
    checks: list[Callable[[], CheckResult]] = []

    # Stage A — Host prerequisites
    s = get_settings()
    access_mode = str(getattr(s, "access_mode", "") or getattr(s, "remote_access_mode", "off"))
    checks.append(lambda: check_docker_installed())
    checks.append(lambda: check_nvidia_container_toolkit(require_gpu=require_gpu))
    checks.append(lambda: check_tailscale_installed(access_mode=access_mode))

    # Stage B — Inside-container prerequisites
    checks.append(lambda: _decorate(check_import_smoke(), stage="B", required=True, category="container"))
    checks.append(lambda: _decorate(check_ffmpeg(), stage="B", required=True, category="container"))
    checks.append(lambda: _decorate(check_torch_cuda(require_gpu=require_gpu), stage="B", category="container"))
    checks.append(lambda: _decorate(check_db_writable(), stage="B", required=True, category="container"))
    checks.append(lambda: _decorate(check_writable_dirs(), stage="B", required=True, category="container"))
    for fn in build_feature_import_checks():
        checks.append(lambda fn=fn: _decorate(fn(), stage="B", category="container"))
    checks.append(lambda: _decorate(check_queue_redis(), stage="B", category="container"))

    # Stage C — Security prerequisites
    checks.append(lambda: _decorate(check_security_secrets(), stage="C", category="security"))
    checks.append(lambda: _decorate(check_remote_access_posture(), stage="C", category="security"))

    # Stage D — Feature checks (models + readiness)
    for fn in build_model_requirement_checks(mode=selected_pipeline_mode()):
        checks.append(lambda fn=fn: _decorate(fn(), stage="D", category="features"))
    checks.append(lambda: _decorate(check_high_mode_readiness(), stage="D", category="features"))
    checks.append(lambda: _decorate(check_ntfy(), stage="D", category="features"))
    checks.append(lambda: _decorate(check_turn(), stage="D", category="features"))

    # Stage E — Smoke test
    if include_smoke:
        checks.append(lambda: _decorate(check_full_job(), stage="E", category="smoke"))

    return checks


def run_doctor(
    *,
    require_gpu: bool = False,
    report_dir: Path | None = None,
    report_path: Path | None = None,
    include_smoke: bool = True,
) -> tuple[DoctorReport, Path, Path]:
    checks = build_setup_checks(require_gpu=require_gpu, include_smoke=include_smoke)
    report = run_checks(checks)
    text = format_report_text(report)
    json_data = format_report_json(report)
    if report_path is not None:
        txt_path = Path(report_path)
        json_path = txt_path.with_suffix(txt_path.suffix + ".json")
    else:
        txt_path, json_path = default_report_paths(report_dir=report_dir)
    write_report(txt_path, text=text, json_data=json_data)
    return report, txt_path, json_path
