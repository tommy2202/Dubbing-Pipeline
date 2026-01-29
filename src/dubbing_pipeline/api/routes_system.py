from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from dubbing_pipeline.api.deps import Identity, require_role
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.api.remote_access import resolve_access_posture
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.modes import HardwareCaps, resolve_effective_settings
from dubbing_pipeline.security import policy
from dubbing_pipeline.security.runtime_db import UnsafeRuntimeDbPath, assert_safe_runtime_db_path

router = APIRouter(
    prefix="/api/system",
    tags=["system"],
    dependencies=[
        Depends(policy.require_request_allowed),
        Depends(policy.require_invite_member),
    ],
)


def _can_import(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _secret_configured(secret: object | None) -> bool:
    if secret is None:
        return False
    try:
        # SecretStr
        val = secret.get_secret_value()  # type: ignore[attr-defined]
        return bool(str(val or "").strip())
    except Exception:
        return bool(str(secret).strip())


def _whisper_cache_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        env = os.environ.get("WHISPER_CACHE_DIR", "")
        if env:
            dirs.append(Path(env).expanduser().resolve())
    except Exception:
        pass
    try:
        dirs.append((Path.home() / ".cache" / "whisper").resolve())
    except Exception:
        pass
    return dirs


def _whisper_model_cached(model_name: str) -> bool:
    for root in _whisper_cache_dirs():
        try:
            if (root / f"{model_name}.pt").exists():
                return True
        except Exception:
            continue
    return False


def _hf_cache_root(s) -> Path:
    try:
        if getattr(s, "transformers_cache", None):
            return Path(str(s.transformers_cache)).expanduser().resolve()
    except Exception:
        pass
    try:
        return (Path(str(s.hf_home)) / "hub").expanduser().resolve()
    except Exception:
        return (Path.home() / ".cache" / "huggingface" / "hub").resolve()


def _hf_model_cached(model_id: str, s) -> bool:
    try:
        root = _hf_cache_root(s)
        key = f"models--{str(model_id).replace('/', '--')}"
        return (root / key).exists()
    except Exception:
        return False


@router.get("/readiness")
async def system_readiness(_: Identity = Depends(require_role(Role.admin))) -> dict[str, Any]:
    s = get_settings()
    items: list[dict[str, Any]] = []

    def add_item(
        *,
        key: str,
        label: str,
        status: str,
        reason: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        items.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "reason": reason,
                "action": action,
                "details": details or {},
            }
        )

    # GPU / CUDA
    torch_installed = _can_import("torch")
    cuda_available = False
    if torch_installed:
        try:
            import torch  # type: ignore

            cuda_available = bool(torch.cuda.is_available())
        except Exception:
            cuda_available = False
    if not torch_installed:
        add_item(
            key="gpu_cuda",
            label="GPU / CUDA",
            status="Missing",
            reason="PyTorch not installed",
            action="Install torch with CUDA support or use CPU mode.",
            details={"torch_installed": False, "cuda_available": False},
        )
    elif not cuda_available:
        add_item(
            key="gpu_cuda",
            label="GPU / CUDA",
            status="Disabled",
            reason="CUDA not available",
            action="Install NVIDIA drivers/CUDA or run in CPU mode.",
            details={"torch_installed": True, "cuda_available": False},
        )
    else:
        add_item(
            key="gpu_cuda",
            label="GPU / CUDA",
            status="OK",
            reason="CUDA available",
            action="",
            details={"torch_installed": True, "cuda_available": True},
        )

    # Whisper models + mode mapping
    whisper_installed = _can_import("whisper")
    caps = HardwareCaps.detect()
    eff_high = resolve_effective_settings(mode="high", base={}, overrides={}, caps=caps)
    eff_med = resolve_effective_settings(mode="medium", base={}, overrides={}, caps=caps)
    eff_low = resolve_effective_settings(mode="low", base={}, overrides={}, caps=caps)
    models_by_mode = {
        "high": eff_high.asr_model,
        "medium": eff_med.asr_model,
        "low": eff_low.asr_model,
    }
    selected = sorted({v for v in models_by_mode.values() if v})
    cached = {m: _whisper_model_cached(m) for m in selected}
    allow_egress = bool(getattr(s, "allow_egress", True))
    if not whisper_installed:
        add_item(
            key="whisper_models",
            label="Whisper models",
            status="Missing",
            reason="whisper not installed",
            action="Install openai-whisper to enable ASR.",
            details={"models_by_mode": models_by_mode, "models_cached": cached},
        )
    else:
        missing = [m for m, ok in cached.items() if not ok]
        if missing and not allow_egress:
            add_item(
                key="whisper_models",
                label="Whisper models",
                status="Disabled",
                reason=f"Missing weights (offline): {', '.join(missing)}",
                action="Download models or set ALLOW_EGRESS=1 for on-demand downloads.",
                details={
                    "models_by_mode": models_by_mode,
                    "models_cached": cached,
                    "allow_egress": allow_egress,
                    "cache_dirs": [str(p) for p in _whisper_cache_dirs()],
                },
            )
        elif missing:
            add_item(
                key="whisper_models",
                label="Whisper models",
                status="OK",
                reason=f"Missing cached weights (download on demand): {', '.join(missing)}",
                action="Optional: pre-download models to avoid first-run delay.",
                details={
                    "models_by_mode": models_by_mode,
                    "models_cached": cached,
                    "allow_egress": allow_egress,
                },
            )
        else:
            add_item(
                key="whisper_models",
                label="Whisper models",
                status="OK",
                reason="Cached models available",
                action="",
                details={"models_by_mode": models_by_mode, "models_cached": cached},
            )

    # Translation: whisper translate
    whisper_model = str(getattr(s, "whisper_model", "medium") or "medium")
    whisper_cached = _whisper_model_cached(whisper_model)
    if not whisper_installed:
        add_item(
            key="translation_whisper",
            label="Translation (Whisper)",
            status="Missing",
            reason="whisper not installed",
            action="Install openai-whisper to enable whisper translate (EN target).",
            details={"whisper_model": whisper_model},
        )
    elif not whisper_cached and not allow_egress:
        add_item(
            key="translation_whisper",
            label="Translation (Whisper)",
            status="Disabled",
            reason="Whisper weights missing (offline)",
            action="Cache Whisper model weights or allow egress.",
            details={"whisper_model": whisper_model, "allow_egress": allow_egress},
        )
    else:
        add_item(
            key="translation_whisper",
            label="Translation (Whisper)",
            status="OK",
            reason="Available for EN target",
            action="",
            details={"whisper_model": whisper_model, "cached": whisper_cached},
        )

    # Translation: offline models (Marian/NLLB)
    transformers_installed = _can_import("transformers")
    if not transformers_installed:
        add_item(
            key="translation_offline",
            label="Translation (Offline models)",
            status="Missing",
            reason="transformers not installed",
            action="Install transformers to enable Marian/NLLB translations.",
            details={},
        )
    elif not allow_egress:
        # Check cache for a representative NLLB model (best-effort).
        nllb_cached = _hf_model_cached("facebook/nllb-200-distilled-600M", s)
        if not nllb_cached:
            add_item(
                key="translation_offline",
                label="Translation (Offline models)",
                status="Disabled",
                reason="Offline mode and models not cached",
                action="Pre-download models or enable ALLOW_EGRESS=1.",
                details={"cache_root": str(_hf_cache_root(s)), "nllb_cached": nllb_cached},
            )
        else:
            add_item(
                key="translation_offline",
                label="Translation (Offline models)",
                status="OK",
                reason="Cached models available (offline)",
                action="",
                details={"cache_root": str(_hf_cache_root(s)), "nllb_cached": nllb_cached},
            )
    else:
        add_item(
            key="translation_offline",
            label="Translation (Offline models)",
            status="OK",
            reason="Download on demand enabled",
            action="Optional: pre-download models to avoid first-run delay.",
            details={"allow_egress": allow_egress, "cache_root": str(_hf_cache_root(s))},
        )

    # XTTS / Coqui TOS
    tts_provider = str(getattr(s, "tts_provider", "auto") or "auto").lower()
    coqui_ok = bool(getattr(s, "coqui_tos_agreed", False))
    tts_installed = _can_import("TTS")
    if tts_provider not in {"auto", "xtts"}:
        add_item(
            key="xtts",
            label="XTTS (Coqui)",
            status="Disabled",
            reason=f"TTS_PROVIDER={tts_provider}",
            action="Set TTS_PROVIDER=auto or xtts to enable XTTS.",
            details={"tts_provider": tts_provider},
        )
    elif not coqui_ok:
        add_item(
            key="xtts",
            label="XTTS (Coqui)",
            status="Disabled",
            reason="COQUI_TOS_AGREED=0",
            action="Set COQUI_TOS_AGREED=1 after reviewing the Coqui TOS.",
            details={"coqui_tos_agreed": False},
        )
    elif not tts_installed:
        add_item(
            key="xtts",
            label="XTTS (Coqui)",
            status="Missing",
            reason="Coqui TTS not installed",
            action="Install TTS (pip install TTS) to enable XTTS.",
            details={"tts_provider": tts_provider, "coqui_tos_agreed": coqui_ok},
        )
    else:
        add_item(
            key="xtts",
            label="XTTS (Coqui)",
            status="OK",
            reason="XTTS available",
            action="",
            details={"tts_provider": tts_provider, "coqui_tos_agreed": coqui_ok},
        )

    # Diarization
    diarizer = str(getattr(s, "diarizer", "auto") or "auto").lower()
    enable_pyannote = bool(getattr(s, "enable_pyannote", False))
    hf_token = _secret_configured(getattr(s, "huggingface_token", None) or getattr(s, "hf_token", None))
    pyannote_installed = _can_import("pyannote.audio")
    speechbrain_installed = _can_import("speechbrain")
    if diarizer == "off":
        add_item(
            key="diarization",
            label="Diarization",
            status="Disabled",
            reason="DIARIZER=off",
            action="Set DIARIZER=auto/pyannote/speechbrain to enable diarization.",
            details={"diarizer": diarizer},
        )
    elif diarizer == "pyannote" or enable_pyannote:
        if not hf_token:
            add_item(
                key="diarization",
                label="Diarization",
                status="Disabled",
                reason="Hugging Face token not configured",
                action="Set HUGGINGFACE_TOKEN (or HF_TOKEN) and ENABLE_PYANNOTE=1.",
                details={"diarizer": diarizer, "token_configured": False},
            )
        elif not pyannote_installed:
            add_item(
                key="diarization",
                label="Diarization",
                status="Missing",
                reason="pyannote.audio not installed",
                action="Install pyannote dependencies or switch to DIARIZER=speechbrain/heuristic.",
                details={"diarizer": diarizer, "token_configured": True},
            )
        else:
            add_item(
                key="diarization",
                label="Diarization",
                status="OK",
                reason="pyannote available",
                action="",
                details={"diarizer": diarizer, "token_configured": True},
            )
    else:
        # speechbrain / heuristic fallback path
        if speechbrain_installed:
            add_item(
                key="diarization",
                label="Diarization",
                status="OK",
                reason="speechbrain available",
                action="",
                details={"diarizer": diarizer, "token_configured": hf_token},
            )
        else:
            add_item(
                key="diarization",
                label="Diarization",
                status="OK",
                reason="heuristic fallback (speechbrain missing)",
                action="Install speechbrain for improved diarization.",
                details={"diarizer": diarizer, "token_configured": hf_token},
            )

    # Separation (Demucs)
    separation = str(getattr(s, "separation", "off") or "off").lower()
    enable_demucs = bool(getattr(s, "enable_demucs", False))
    demucs_installed = _can_import("demucs")
    if separation == "off" or not enable_demucs:
        add_item(
            key="separation",
            label="Separation (Demucs)",
            status="Disabled",
            reason="SEPARATION=off or ENABLE_DEMUCS=0",
            action="Set ENABLE_DEMUCS=1 and SEPARATION=demucs to enable.",
            details={"separation": separation, "enable_demucs": enable_demucs},
        )
    elif not demucs_installed:
        add_item(
            key="separation",
            label="Separation (Demucs)",
            status="Missing",
            reason="demucs not installed",
            action="Install demucs or disable separation.",
            details={"separation": separation, "enable_demucs": enable_demucs},
        )
    else:
        add_item(
            key="separation",
            label="Separation (Demucs)",
            status="OK",
            reason="Demucs available",
            action="",
            details={"separation": separation, "enable_demucs": enable_demucs},
        )

    # Lip-sync plugin
    lipsync = str(getattr(s, "lipsync", "off") or "off").lower()
    if lipsync == "off":
        add_item(
            key="lipsync",
            label="Lip-sync (Wav2Lip)",
            status="Disabled",
            reason="LIPSYNC=off",
            action="Set LIPSYNC=wav2lip and provide repo + checkpoint.",
            details={"lipsync": lipsync},
        )
    else:
        try:
            from dubbing_pipeline.plugins.lipsync.wav2lip_plugin import get_wav2lip_plugin

            avail = bool(get_wav2lip_plugin().is_available())
        except Exception:
            avail = False
        if not avail:
            add_item(
                key="lipsync",
                label="Lip-sync (Wav2Lip)",
                status="Missing",
                reason="Wav2Lip repo/checkpoint missing",
                action="Place Wav2Lip under third_party/wav2lip and set WAV2LIP_CHECKPOINT.",
                details={"lipsync": lipsync, "wav2lip_available": False},
            )
        else:
            add_item(
                key="lipsync",
                label="Lip-sync (Wav2Lip)",
                status="OK",
                reason="Wav2Lip available",
                action="",
                details={"lipsync": lipsync, "wav2lip_available": True},
            )

    # Storage backend / safety
    out_root = Path(str(getattr(s, "output_dir", "") or "")).resolve()
    state_root = Path(getattr(s, "state_dir", None) or (out_root / "_state")).resolve()
    jobs_db = state_root / str(getattr(s, "jobs_db_name", "jobs.db") or "jobs.db")
    auth_db = state_root / str(getattr(s, "auth_db_name", "auth.db") or "auth.db")
    safe = True
    reason = "sqlite + file lock"
    action = ""
    try:
        assert_safe_runtime_db_path(
            jobs_db,
            purpose="jobs",
            repo_root=Path(getattr(s, "app_root", Path.cwd())).resolve(),
            allowed_repo_subdirs=[state_root],
        )
        assert_safe_runtime_db_path(
            auth_db,
            purpose="auth",
            repo_root=Path(getattr(s, "app_root", Path.cwd())).resolve(),
            allowed_repo_subdirs=[state_root],
        )
    except UnsafeRuntimeDbPath as ex:
        safe = False
        reason = str(ex)
        action = "Set DUBBING_STATE_DIR under Output/_state (runtime-only)."
    add_item(
        key="storage_backend",
        label="Storage backend",
        status="OK" if safe else "Missing",
        reason=reason if reason else "OK",
        action=action,
        details={
            "backend": "sqlite",
            "single_writer": True,
            "state_dir": str(state_root),
            "jobs_db": str(jobs_db),
            "auth_db": str(auth_db),
        },
    )

    # Retention
    retention_enabled = bool(getattr(s, "retention_enabled", False))
    retention_interval = int(getattr(s, "retention_interval_sec", 0) or 0)
    if retention_enabled and retention_interval > 0:
        add_item(
            key="retention",
            label="Retention",
            status="OK",
            reason="Retention enabled",
            action="",
            details={"enabled": True, "interval_sec": retention_interval},
        )
    else:
        reason = "RETENTION_ENABLED=0" if not retention_enabled else "RETENTION_INTERVAL_SEC=0"
        add_item(
            key="retention",
            label="Retention",
            status="Disabled",
            reason=reason,
            action="Set RETENTION_ENABLED=1 and RETENTION_INTERVAL_SEC>0.",
            details={"enabled": retention_enabled, "interval_sec": retention_interval},
        )

    return {"ok": True, "items": items}


@router.get("/security-posture")
async def system_security_posture(_: Identity = Depends(require_role(Role.admin))) -> dict[str, Any]:
    s = get_settings()
    posture = resolve_access_posture(settings=s)
    base_url = str(getattr(s, "public_base_url", "") or "").strip().rstrip("/")
    return {
        "ok": True,
        "access_mode": str(posture.get("mode") or "off"),
        "access_mode_raw": str(posture.get("access_mode_raw") or ""),
        "remote_access_mode_raw": str(posture.get("remote_access_mode_raw") or ""),
        "host": str(getattr(s, "host", "") or ""),
        "port": int(getattr(s, "port", 0) or 0),
        "public_base_url": base_url,
        "trust_proxy_headers": bool(posture.get("trust_proxy_headers")),
        "effective_trust_proxy_headers": bool(posture.get("effective_trust_proxy_headers")),
        "trusted_proxy_subnets": list(posture.get("trusted_proxy_subnets") or []),
        "allowed_subnets": list(posture.get("allowed_subnets") or []),
        "cloudflare_access_configured": bool(posture.get("cloudflare_access_configured")),
        "warnings": list(posture.get("warnings") or []),
    }
