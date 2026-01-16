from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.models import Job


DiarizationInputKind = Literal["dialogue_stem", "original"]


@dataclass(frozen=True, slots=True)
class DiarizationRouting:
    wav: Path
    kind: DiarizationInputKind
    rel_path: str


def _base_dir_for_job(job: Job, *, base_dir: Path | None) -> Path:
    if base_dir is not None:
        return Path(base_dir).resolve()
    wd = str(getattr(job, "work_dir", "") or "").strip()
    if wd:
        return Path(wd).resolve()
    # Fallback: derive from library paths (best-effort).
    from dubbing_pipeline.library.paths import get_job_output_root

    return get_job_output_root(job)


def _rel_to(base: Path, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(Path(base).resolve())).replace("\\", "/")
    except Exception:
        return str(Path(p).resolve()).replace("\\", "/")


def resolve_diarization_input(
    job: Job,
    *,
    extracted_wav: Path,
    base_dir: Path | None = None,
    separation_enabled: bool | None = None,
) -> DiarizationRouting:
    """
    Canonical diarization input resolver.

    Rules:
    - If separation is enabled AND Output/<job>/stems/dialogue.wav exists -> diarize dialogue stem.
    - Else -> diarize the original extracted wav.

    This function does NOT run separation or validate audio content; it only chooses a path.
    """
    extracted_wav = Path(extracted_wav).resolve()
    bd = _base_dir_for_job(job, base_dir=base_dir)

    if separation_enabled is None:
        # Conservative default: treat separation as enabled only when config says demucs.
        separation_enabled = str(get_settings().separation or "off").strip().lower() == "demucs"

    dialogue = (bd / "stems" / "dialogue.wav").resolve()
    if bool(separation_enabled) and dialogue.exists() and dialogue.is_file():
        return DiarizationRouting(wav=dialogue, kind="dialogue_stem", rel_path=_rel_to(bd, dialogue))

    return DiarizationRouting(wav=extracted_wav, kind="original", rel_path=_rel_to(bd, extracted_wav))


def resolve_diarization_wav(
    job: Job,
    *,
    extracted_wav: Path,
    base_dir: Path | None = None,
    separation_enabled: bool | None = None,
) -> Path:
    """
    Convenience wrapper returning only the WAV path (kept for call-site simplicity).
    """
    return resolve_diarization_input(
        job, extracted_wav=extracted_wav, base_dir=base_dir, separation_enabled=separation_enabled
    ).wav

