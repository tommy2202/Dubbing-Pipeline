from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from anime_v2.api.deps import Identity, require_role
from anime_v2.api.models import Role
from anime_v2.config import get_settings
from anime_v2.ops.storage import ensure_free_space
from anime_v2.runtime.model_manager import ModelManager
from anime_v2.runtime.scheduler import Scheduler

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/state")
async def runtime_state(_: Identity = Depends(require_role(Role.operator))):
    s = Scheduler.instance_optional()
    if s is None:
        return {"ok": False, "detail": "scheduler not installed"}
    return {"ok": True, "state": s.state()}


@router.get("/models")
async def runtime_models(_: Identity = Depends(require_role(Role.admin))):
    s = get_settings()
    mm = ModelManager.instance()
    # Disk usage (best-effort)
    import shutil

    def _du(path: str) -> dict:
        try:
            u = shutil.disk_usage(path)
            free_gb = float(u.free) / (1024**3)
            total_gb = float(u.total) / (1024**3)
            return {"path": path, "free_gb": free_gb, "total_gb": total_gb}
        except Exception:
            return {"path": path, "free_gb": None, "total_gb": None}

    out_dir = str(getattr(s, "output_dir", "") or "")
    hf_home = str(getattr(s, "hf_home", "") or "")
    tts_home = str(getattr(s, "tts_home", "") or "")
    disk = _du(out_dir or "/")
    low_space = False
    try:
        ensure_free_space(min_gb=int(getattr(s, "min_free_gb", 10)), path=Path(out_dir).resolve())
    except Exception:
        low_space = True
    disk["low_space"] = bool(low_space)
    disk["summary"] = (
        f"free {disk['free_gb']:.1f}GB / {disk['total_gb']:.1f}GB" if disk.get("free_gb") else "unknown"
    )

    enabled = bool(getattr(s, "enable_model_downloads", False)) and bool(getattr(s, "allow_egress", True))
    hint = ""
    if not enabled:
        hint = "Set ENABLE_MODEL_DOWNLOADS=1 and ALLOW_EGRESS=1 to allow in-UI prewarm/downloads."

    return {
        "ok": True,
        "paths": {"output_dir": out_dir, "hf_home": hf_home, "tts_home": tts_home, "torch_home": str(s.torch_home)},
        "disk": disk,
        "loaded": mm.state(),
        "downloads": {"enabled": bool(enabled), "hint": hint},
    }


@router.post("/models/prewarm")
async def runtime_models_prewarm(
    request: Request, preset: str = "medium", _: Identity = Depends(require_role(Role.admin))
):
    s = get_settings()
    if not bool(getattr(s, "enable_model_downloads", False)):
        raise HTTPException(status_code=400, detail="Model downloads/prewarm disabled (ENABLE_MODEL_DOWNLOADS=0)")
    if not bool(getattr(s, "allow_egress", True)):
        raise HTTPException(status_code=400, detail="Egress disabled (ALLOW_EGRESS=0)")

    preset = str(preset or "medium").strip().lower()
    if preset not in {"low", "medium", "high"}:
        raise HTTPException(status_code=400, detail="preset must be low|medium|high")

    # Choose conservative defaults
    whisper = {"low": "small", "medium": "medium", "high": "large-v3"}[preset]
    tts = str(getattr(s, "tts_model", "tts_models/multilingual/multi-dataset/xtts_v2") or "").strip()

    import threading
    from uuid import uuid4

    task_id = uuid4().hex[:12]
    mm = ModelManager.instance()

    def _run():
        try:
            mm.prewarm()  # respects PREWARM_* if user set it
        except Exception:
            pass
        # Also try explicit preset models
        try:
            with mm.acquire_whisper(whisper, "cpu"):
                pass
        except Exception:
            pass
        try:
            with mm.acquire_tts(tts, "cpu"):
                pass
        except Exception:
            pass

    th = threading.Thread(target=_run, name=f"model_prewarm:{task_id}", daemon=True)
    th.start()
    return {"ok": True, "task_id": task_id, "preset": preset, "whisper": whisper, "tts": tts}
