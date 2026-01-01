from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal

from anime_v2.config import get_settings
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class GpuStatus:
    util_ratio: float  # 0..1
    mem_ratio: float  # 0..1


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _read_gpu_status() -> GpuStatus | None:
    """
    Best-effort GPU telemetry.

    Uses nvidia-smi if present:
      utilization.gpu (0-100), memory.used (MiB), memory.total (MiB)

    Returns None if unavailable.
    """
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).decode("utf-8", errors="replace")
    except Exception:
        return None

    # If multiple GPUs, be conservative and take the "most saturated".
    worst_util = 0.0
    worst_mem = 0.0
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            util = float(parts[0]) / 100.0
            mem_used = float(parts[1])
            mem_total = float(parts[2])
            mem = 0.0 if mem_total <= 0 else mem_used / mem_total
            worst_util = max(worst_util, util)
            worst_mem = max(worst_mem, mem)
        except Exception:
            continue
    if worst_util == 0.0 and worst_mem == 0.0:
        # Could be a parse failure; treat as unknown.
        return None
    return GpuStatus(util_ratio=worst_util, mem_ratio=worst_mem)


def pick_device(prefer: Literal["auto", "cuda", "cpu"] = "auto") -> str:
    """
    Choose device for ML inference.

    - prefer=cpu => cpu
    - prefer=cuda => cuda if available else cpu
    - prefer=auto => cuda only if:
        - CUDA available
        - GPU util and mem below thresholds (GPU_UTIL_MAX, GPU_MEM_MAX_RATIO)
        - and GPU telemetry is available; otherwise fall back to cpu (conservative)
    """
    prefer = (prefer or "auto").lower().strip()
    if prefer not in {"auto", "cuda", "cpu"}:
        prefer = "auto"

    if prefer == "cpu":
        return "cpu"

    if not _cuda_available():
        return "cpu"

    if prefer == "cuda":
        return "cuda"

    s = get_settings()
    util_max = float(s.gpu_util_max)
    mem_max = float(s.gpu_mem_max_ratio)
    st = _read_gpu_status()
    if st is None:
        logger.warning("gpu_status_unavailable; falling back to cpu")
        return "cpu"
    if st.util_ratio >= util_max or st.mem_ratio >= mem_max:
        logger.info(
            "gpu_saturated; using cpu",
            util=st.util_ratio,
            mem=st.mem_ratio,
            util_max=util_max,
            mem_max=mem_max,
        )
        return "cpu"
    return "cuda"
