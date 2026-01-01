from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.ffmpeg import ensure_wav_44k_stereo
from anime_v2.utils.io import atomic_copy, atomic_write_text
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class SeparationResult:
    dialogue_wav: Path
    background_wav: Path
    meta_path: Path
    cached: bool


def demucs_available() -> bool:
    """
    Return True when demucs is importable (optional dependency).
    """
    try:
        import demucs  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


def _key_for(input_wav: Path, *, model: str, device: str, stems: str) -> str:
    # Small, stable hash key; no heavy deps.
    from hashlib import sha256

    p = Path(input_wav)
    h = sha256()
    h.update(p.read_bytes())
    h.update(f"|model={model}|device={device}|stems={stems}|v=1".encode())
    return h.hexdigest()[:32]


def separate_dialogue(
    input_wav: Path,
    out_dir: Path,
    *,
    model: str = "htdemucs",
    device: str = "auto",
    stems: str = "vocals",
    cache_dir: Path | None = None,
    timeout_s: int = 1800,
) -> SeparationResult:
    """
    Separate dialogue vs background using Demucs when installed.

    Outputs:
      - dialogue.wav: best approximation of dialogue (Demucs `vocals.wav`)
      - background.wav: bed (Demucs `no_vocals.wav`)

    Caching:
      - keyed by input_wav content hash + model/device/stems
      - cache stores canonical WAVs + meta.json, then copies into out_dir
    """
    input_wav = Path(input_wav)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if stems != "vocals":
        stems = "vocals"

    if not demucs_available():
        raise RuntimeError("demucs not installed (install extras: .[mixing])")

    s = get_settings()
    cache_root = Path(cache_dir or (Path(s.cache_dir) / "separation")).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    key = _key_for(input_wav, model=str(model), device=str(device), stems=stems)
    slot = cache_root / key
    slot.mkdir(parents=True, exist_ok=True)

    cached_dialogue = slot / "dialogue.wav"
    cached_background = slot / "background.wav"
    cached_meta = slot / "meta.json"

    if cached_dialogue.exists() and cached_background.exists() and cached_meta.exists():
        # Copy into requested out_dir locations
        dlg = out_dir / "dialogue.wav"
        bg = out_dir / "background.wav"
        atomic_copy(cached_dialogue, dlg)
        atomic_copy(cached_background, bg)
        atomic_copy(cached_meta, out_dir / "meta.json")
        return SeparationResult(
            dialogue_wav=dlg, background_wav=bg, meta_path=out_dir / "meta.json", cached=True
        )

    # Demucs is much happier with 44.1kHz stereo WAV
    demucs_in = slot / "demucs_in.wav"
    if not demucs_in.exists():
        ensure_wav_44k_stereo(input_wav, demucs_in, timeout_s=120)

    demucs_out = slot / "demucs_out"
    demucs_out.mkdir(parents=True, exist_ok=True)

    # Demucs CLI writes: <out>/<model>/<track>/vocals.wav, no_vocals.wav
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        str(model),
        "--two-stems",
        "vocals",
        "-o",
        str(demucs_out),
        str(demucs_in),
    ]
    # Device selection is optional; only pass when non-auto to avoid CLI incompatibilities.
    dev = str(device).lower().strip()
    if dev in {"cpu", "cuda"}:
        cmd[2:2] = ["--device", dev]  # insert after "-m demucs"

    logger.info("[v2] separation: demucs start", model=str(model), device=str(device), key=key)
    # Use subprocess directly here; we want captured stderr for demucs errors.
    import subprocess

    p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    if p.returncode != 0:
        raise RuntimeError(
            "demucs failed "
            f"(exit={p.returncode})\n"
            f"cmd={cmd}\n"
            f"stderr_tail={(p.stderr or '')[-4000:]}"
        )

    track = demucs_in.stem
    vocals = demucs_out / str(model) / track / "vocals.wav"
    no_vocals = demucs_out / str(model) / track / "no_vocals.wav"
    if not vocals.exists() or not no_vocals.exists():
        # Fallback search
        found_v = None
        found_nv = None
        for fp in demucs_out.rglob("vocals.wav"):
            found_v = fp
            break
        for fp in demucs_out.rglob("no_vocals.wav"):
            found_nv = fp
            break
        vocals = found_v or vocals
        no_vocals = found_nv or no_vocals

    if not vocals.exists() or not no_vocals.exists():
        raise RuntimeError("demucs finished but stems not found (vocals/no_vocals)")

    # Write cache canonicals
    atomic_copy(vocals, cached_dialogue)
    atomic_copy(no_vocals, cached_background)
    meta: dict[str, Any] = {
        "key": key,
        "model": str(model),
        "device": str(device),
        "stems": stems,
        "input": str(input_wav),
        "cache_dir": str(slot),
    }
    atomic_write_text(cached_meta, json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    # Copy into requested out_dir locations
    dlg = out_dir / "dialogue.wav"
    bg = out_dir / "background.wav"
    atomic_copy(cached_dialogue, dlg)
    atomic_copy(cached_background, bg)
    atomic_copy(cached_meta, out_dir / "meta.json")

    logger.info("[v2] separation: demucs ok", dialogue=str(dlg), background=str(bg))
    return SeparationResult(
        dialogue_wav=dlg, background_wav=bg, meta_path=out_dir / "meta.json", cached=False
    )
