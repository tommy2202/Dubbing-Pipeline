from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dubbing_pipeline.audio.separation import demucs_available
from dubbing_pipeline.config import get_settings
from dubbing_pipeline.modes import HardwareCaps, resolve_effective_settings
from dubbing_pipeline.plugins.lipsync.registry import resolve_lipsync_plugin
from dubbing_pipeline.stages.diarization import DiarizeConfig


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status(ok: bool, *, disabled: bool = False) -> str:
    if ok:
        return "OK"
    return "Disabled" if disabled else "Missing"


def _item(name: str, *, ok: bool, disabled: bool, reason: str, action: str) -> dict[str, Any]:
    return {
        "name": str(name),
        "status": _status(ok, disabled=disabled),
        "reason": str(reason),
        "action": str(action),
    }


def _can_import(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _torch_info() -> dict[str, Any]:
    try:
        import torch  # type: ignore

        return {
            "available": True,
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_version": str(getattr(torch.version, "cuda", "") or ""),
        }
    except Exception:
        return {"available": False, "cuda_available": False, "cuda_version": ""}


def _whisper_cache_roots() -> list[Path]:
    s = get_settings()
    roots: list[Path] = []
    env = os.environ.get("WHISPER_CACHE_DIR")
    if env:
        roots.append(Path(env).expanduser().resolve())
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        roots.append(Path(xdg).expanduser().resolve() / "whisper")
    roots.append(Path.home() / ".cache" / "whisper")
    try:
        roots.append(Path(s.torch_home).expanduser().resolve() / "hub" / "checkpoints")
    except Exception:
        pass
    # de-dupe
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def _whisper_model_files(model: str) -> list[str]:
    m = str(model or "").strip().lower()
    if m in {"large", "large-v2", "large-v3"}:
        return ["large.pt", "large-v2.pt", "large-v3.pt"]
    return [f"{m}.pt"] if m else []


def whisper_model_cached(model: str) -> bool:
    files = _whisper_model_files(model)
    if not files:
        return False
    for root in _whisper_cache_roots():
        for fn in files:
            if (root / fn).exists():
                return True
    return False


def _hf_cache_roots() -> list[Path]:
    s = get_settings()
    roots: list[Path] = []
    env = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if env:
        roots.append(Path(env).expanduser().resolve())
    env2 = os.environ.get("TRANSFORMERS_CACHE")
    if env2:
        roots.append(Path(env2).expanduser().resolve())
    if s.transformers_cache:
        roots.append(Path(s.transformers_cache).expanduser().resolve())
    if s.hf_home:
        roots.append(Path(s.hf_home).expanduser().resolve() / "hub")
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def hf_model_cached(model_id: str) -> bool:
    mid = str(model_id or "").strip()
    if not mid:
        return False
    folder = "models--" + mid.replace("/", "--")
    for root in _hf_cache_roots():
        if (root / folder).exists():
            return True
    return False


def hf_any_cached(prefix: str) -> bool:
    pref = str(prefix or "").strip()
    if not pref:
        return False
    for root in _hf_cache_roots():
        try:
            for p in root.glob(pref):
                if p.exists():
                    return True
        except Exception:
            continue
    return False


def tts_model_cached(model_name: str) -> bool:
    s = get_settings()
    home = Path(s.tts_home).expanduser().resolve()
    if not home.exists():
        return False
    name = str(model_name or "").strip()
    if not name:
        return False
    cand = [name, name.replace("/", "--")]
    for c in cand:
        if (home / c).exists():
            return True
    return False


def _effective_asr_by_mode() -> list[dict[str, Any]]:
    s = get_settings()
    caps = HardwareCaps.detect()
    base = {
        "diarizer": str(getattr(s, "diarizer", "auto")),
        "speaker_smoothing": bool(getattr(s, "speaker_smoothing", False)),
        "voice_memory": bool(getattr(s, "voice_memory", False)),
        "voice_mode": str(getattr(s, "voice_mode", "clone")),
        "voice_clone_two_pass": bool(getattr(s, "voice_clone_two_pass", False)),
        "music_detect": bool(getattr(s, "music_detect", False)),
        "separation": str(getattr(s, "separation", "off")),
        "mix_mode": str(getattr(s, "mix_mode", "legacy")),
        "timing_fit": bool(getattr(s, "timing_fit", False)),
        "pacing": bool(getattr(s, "pacing", False)),
        "qa": bool(getattr(s, "qa", False)),
        "director": bool(getattr(s, "director", False)),
        "multitrack": bool(getattr(s, "multitrack", False)),
        "stream_context_seconds": float(
            getattr(s, "stream_context_seconds", 15.0) or 15.0
        ),
    }
    out: list[dict[str, Any]] = []
    for mode in ("low", "medium", "high"):
        eff = resolve_effective_settings(mode=mode, base=base, overrides={}, caps=caps)
        out.append(
            {
                "mode": mode,
                "asr_model": str(eff.asr_model),
                "cached": bool(whisper_model_cached(str(eff.asr_model))),
                "reasons": list(eff.reasons),
            }
        )
    return out


def collect_readiness() -> dict[str, Any]:
    s = get_settings()
    caps = HardwareCaps.detect()
    torch_info = _torch_info()

    mt_engine = str(getattr(s, "mt_engine", "auto") or "auto").strip().lower()
    if mt_engine not in {"auto", "whisper", "marian", "nllb"}:
        mt_engine = "auto"

    sections: list[dict[str, Any]] = []

    gpu_items = []
    if not torch_info.get("available"):
        gpu_items.append(
            _item(
                "CUDA",
                ok=False,
                disabled=False,
                reason="torch not installed",
                action="Install PyTorch (CUDA build) or use DEVICE=cpu.",
            )
        )
    elif torch_info.get("cuda_available"):
        gpu_items.append(
            _item(
                "CUDA",
                ok=True,
                disabled=False,
                reason="CUDA available",
                action="No action needed.",
            )
        )
    else:
        gpu_items.append(
            _item(
                "CUDA",
                ok=False,
                disabled=False,
                reason="CUDA not available",
                action="Install NVIDIA drivers/CUDA or use DEVICE=cpu.",
            )
        )

    sections.append({"title": "GPU/CUDA", "items": gpu_items})

    whisper_items = []
    for model in ("tiny", "small", "medium", "large"):
        if not caps.has_whisper:
            whisper_items.append(
                _item(
                    f"Whisper {model}",
                    ok=False,
                    disabled=False,
                    reason="whisper not installed",
                    action="Install openai-whisper and cache model weights.",
                )
            )
            continue
        cached = whisper_model_cached(model)
        whisper_items.append(
            _item(
                f"Whisper {model}",
                ok=bool(cached),
                disabled=False,
                reason="weights cached" if cached else "model weights not found",
                action="Enable downloads or prewarm the model.",
            )
        )

    sections.append(
        {
            "title": "Whisper ASR",
            "items": whisper_items,
            "selected_by_mode": _effective_asr_by_mode(),
        }
    )

    transformers_ok = _can_import("transformers")
    marian_cached = hf_any_cached("models--Helsinki-NLP--opus-mt-*")
    nllb_cached = hf_model_cached("facebook/nllb-200-distilled-600M")

    def _mt_item(name: str, engine: str, cached: bool) -> dict[str, Any]:
        if mt_engine not in {"auto", engine}:
            return _item(
                name,
                ok=False,
                disabled=True,
                reason=f"MT_ENGINE={mt_engine}",
                action=f"Set MT_ENGINE=auto or {engine}.",
            )
        if not transformers_ok:
            return _item(
                name,
                ok=False,
                disabled=False,
                reason="transformers not installed",
                action="Install transformers + sentencepiece.",
            )
        if not cached:
            return _item(
                name,
                ok=False,
                disabled=False,
                reason="model weights not cached",
                action="Enable downloads or prewarm MT models.",
            )
        return _item(name, ok=True, disabled=False, reason="available", action="No action needed.")

    translation_items = [
        _mt_item("Marian MT", "marian", marian_cached),
        _mt_item("NLLB MT", "nllb", nllb_cached),
    ]

    if mt_engine not in {"auto", "whisper"}:
        translation_items.append(
            _item(
                "Whisper translate",
                ok=False,
                disabled=True,
                reason=f"MT_ENGINE={mt_engine}",
                action="Set MT_ENGINE=auto or whisper (tgt_lang must be en).",
            )
        )
    elif not caps.has_whisper:
        translation_items.append(
            _item(
                "Whisper translate",
                ok=False,
                disabled=False,
                reason="whisper not installed",
                action="Install openai-whisper and cache model weights.",
            )
        )
    else:
        any_cached = any(whisper_model_cached(m) for m in ("tiny", "small", "medium", "large"))
        translation_items.append(
            _item(
                "Whisper translate",
                ok=bool(any_cached),
                disabled=False,
                reason="weights cached" if any_cached else "model weights not found",
                action="Ensure tgt_lang=en and cache a Whisper model.",
            )
        )

    sections.append({"title": "Translation", "items": translation_items})

    tts_provider = str(getattr(s, "tts_provider", "auto") or "auto").strip().lower()
    tts_model = str(getattr(s, "tts_model", "") or "")
    tts_cached = tts_model_cached(tts_model)

    if tts_provider not in {"auto", "xtts"}:
        xtts_item = _item(
            "XTTS (Coqui)",
            ok=False,
            disabled=True,
            reason=f"TTS_PROVIDER={tts_provider}",
            action="Set TTS_PROVIDER=auto or xtts.",
        )
    elif not caps.has_coqui_tts:
        xtts_item = _item(
            "XTTS (Coqui)",
            ok=False,
            disabled=False,
            reason="TTS package not installed",
            action="Install Coqui TTS (pip install TTS).",
        )
    elif not bool(getattr(s, "coqui_tos_agreed", False)):
        xtts_item = _item(
            "XTTS (Coqui)",
            ok=False,
            disabled=True,
            reason="COQUI_TOS_AGREED=0",
            action="Set COQUI_TOS_AGREED=1 after reviewing the license.",
        )
    elif not tts_cached:
        xtts_item = _item(
            "XTTS (Coqui)",
            ok=False,
            disabled=False,
            reason="model weights not cached",
            action="Enable downloads or prewarm the XTTS model.",
        )
    else:
        xtts_item = _item(
            "XTTS (Coqui)",
            ok=True,
            disabled=False,
            reason="COQUI_TOS_AGREED=1",
            action="No action needed.",
        )

    sections.append({"title": "TTS", "items": [xtts_item]})

    diarizer = str(getattr(s, "diarizer", "auto") or "auto").strip().lower()
    enable_py = bool(getattr(s, "enable_pyannote", False))
    token_configured = bool(getattr(s, "huggingface_token", None) or getattr(s, "hf_token", None))
    pyannote_ok = enable_py and caps.has_pyannote and token_configured

    if diarizer == "off":
        py_item = _item(
            "Pyannote diarization",
            ok=False,
            disabled=True,
            reason="DIARIZER=off",
            action="Set DIARIZER=auto or pyannote.",
        )
    elif not enable_py:
        py_item = _item(
            "Pyannote diarization",
            ok=False,
            disabled=True,
            reason="ENABLE_PYANNOTE=0",
            action="Set ENABLE_PYANNOTE=1 and configure a HF token.",
        )
    elif not caps.has_pyannote:
        py_item = _item(
            "Pyannote diarization",
            ok=False,
            disabled=False,
            reason="pyannote.audio not installed",
            action="Install diarization extras (pip install -e .[diarization]).",
        )
    elif not token_configured:
        py_item = _item(
            "Pyannote diarization",
            ok=False,
            disabled=False,
            reason="HF token configured: no",
            action="Set HUGGINGFACE_TOKEN or HF_TOKEN in .env.secrets.",
        )
    else:
        py_item = _item(
            "Pyannote diarization",
            ok=True,
            disabled=False,
            reason="HF token configured: yes",
            action="No action needed.",
        )

    speechbrain_ok = _can_import("speechbrain")
    if diarizer == "off":
        sb_item = _item(
            "SpeechBrain/heuristic",
            ok=False,
            disabled=True,
            reason="DIARIZER=off",
            action="Set DIARIZER=auto or speechbrain.",
        )
    elif speechbrain_ok:
        sb_item = _item(
            "SpeechBrain/heuristic",
            ok=True,
            disabled=False,
            reason="speechbrain available",
            action="No action needed.",
        )
    else:
        sb_item = _item(
            "SpeechBrain/heuristic",
            ok=True,
            disabled=False,
            reason="speechbrain missing; heuristic fallback",
            action="Install speechbrain for higher quality clustering.",
        )

    sections.append({"title": "Diarization", "items": [py_item, sb_item]})

    sep_enabled = str(getattr(s, "separation", "off") or "off").strip().lower() == "demucs"
    if not sep_enabled and not bool(getattr(s, "enable_demucs", False)):
        demucs_item = _item(
            "Demucs separation",
            ok=False,
            disabled=True,
            reason="SEPARATION=off",
            action="Set SEPARATION=demucs (or use high mode).",
        )
    elif not demucs_available():
        demucs_item = _item(
            "Demucs separation",
            ok=False,
            disabled=False,
            reason="demucs not installed",
            action="Install mixing extras (pip install -e .[mixing]).",
        )
    else:
        demucs_item = _item(
            "Demucs separation",
            ok=True,
            disabled=False,
            reason="available",
            action="No action needed.",
        )
    sections.append({"title": "Demucs", "items": [demucs_item]})

    lipsync_mode = str(getattr(s, "lipsync", "off") or "off").strip().lower()
    if lipsync_mode in {"off", "none", ""}:
        lipsync_item = _item(
            "Lip-sync plugin",
            ok=False,
            disabled=True,
            reason="LIPSYNC=off",
            action="Set LIPSYNC=wav2lip and configure paths.",
        )
    else:
        plugin = resolve_lipsync_plugin(
            lipsync_mode,
            wav2lip_dir=getattr(s, "wav2lip_dir", None),
            wav2lip_checkpoint=getattr(s, "wav2lip_checkpoint", None),
        )
        if plugin is None:
            lipsync_item = _item(
                "Lip-sync plugin",
                ok=False,
                disabled=False,
                reason="unsupported plugin",
                action="Set LIPSYNC=wav2lip and provide Wav2Lip files.",
            )
        elif not plugin.is_available():
            lipsync_item = _item(
                "Lip-sync plugin",
                ok=False,
                disabled=False,
                reason="Wav2Lip repo/checkpoint missing",
                action="Set WAV2LIP_DIR and WAV2LIP_CHECKPOINT.",
            )
        else:
            lipsync_item = _item(
                "Lip-sync plugin",
                ok=True,
                disabled=False,
                reason="available",
                action="No action needed.",
            )
    sections.append({"title": "Lip-sync", "items": [lipsync_item]})

    return {
        "ok": True,
        "generated_at": _now_iso(),
        "mt_engine": mt_engine,
        "sections": sections,
    }
