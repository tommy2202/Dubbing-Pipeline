from __future__ import annotations

import json
from pathlib import Path

import pytest

from dubbing_pipeline.modes import HardwareCaps, resolve_effective_settings


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_matrix(md: str) -> dict[str, dict[str, str]]:
    """
    Parse docs/mode_contract_matrix.md table into:
      feature -> {high: cell, medium: cell, low: cell}
    """
    lines = [ln.rstrip("\n") for ln in md.splitlines()]
    rows = []
    for ln in lines:
        if not ln.strip().startswith("|"):
            continue
        parts = [p.strip() for p in ln.strip().strip("|").split("|")]
        if len(parts) != 4:
            continue
        if parts[0].lower() == "feature":
            continue
        if parts[0].startswith("---"):
            continue
        rows.append(parts)
    out: dict[str, dict[str, str]] = {}
    for feat, high, med, low in rows:
        out[feat] = {"high": high, "medium": med, "low": low}
    return out


def _expected_from_matrix(
    matrix: dict[str, dict[str, str]], *, mode: str, gpu: bool
) -> dict[str, object]:
    """
    Convert the table cells to concrete expectations for our resolver output.
    """
    mode = mode.lower()
    exp: dict[str, object] = {}

    # ASR model cell is special
    asr_cell = matrix["**ASR model default**"][mode]
    if "large-v3" in asr_cell and "else" in asr_cell:
        exp["asr_model"] = "large-v3" if gpu else "medium"
    elif "medium" in asr_cell:
        exp["asr_model"] = "medium"
    elif "small" in asr_cell:
        exp["asr_model"] = "small"

    # boolean-ish cells
    def cell_bool(cell: str) -> object:
        c = cell.lower()
        if c.startswith("on"):
            return True
        if c.startswith("off"):
            return False
        return "as_base"

    exp["speaker_smoothing"] = (
        bool(cell_bool(matrix["**Speaker smoothing**"][mode]))
        if mode == "high"
        else (False if "off" in matrix["**Speaker smoothing**"][mode].lower() else "as_base")
    )
    exp["voice_memory"] = "on" in matrix["**Voice memory**"][mode].lower()

    # diarizer: low expects off
    exp["diarizer"] = (
        "off" if "off" in matrix["**Diarization**"][mode].lower() and mode == "low" else "auto"
    )

    # separation
    sep_cell = matrix["**Separation (Demucs)**"][mode].lower()
    if mode == "high" and sep_cell.startswith("on"):
        exp["separation"] = "demucs"
    else:
        exp["separation"] = "off"

    # mix_mode
    if mode == "high":
        exp["mix_mode"] = "enhanced"
    elif mode == "low":
        exp["mix_mode"] = "legacy"
    else:
        exp["mix_mode"] = "as_base"

    # timing/pacing
    exp["timing_fit"] = mode == "high"
    exp["pacing"] = True if mode == "high" else ("as_base" if mode == "medium" else False)

    # qa/director/multitrack
    exp["qa"] = mode == "high"
    exp["director"] = mode == "high"
    exp["multitrack"] = mode == "high"

    # voice mode default
    if mode == "high":
        exp["voice_mode"] = "clone"
    elif mode == "low":
        exp["voice_mode"] = "single"
    else:
        exp["voice_mode"] = "as_base"

    # music detection default: always off by mode
    exp["music_detect"] = False
    return exp


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("mode", ["high", "medium", "low"])
def test_mode_contract_matches_docs_matrix(mode: str, gpu: bool) -> None:
    md = (_repo_root() / "docs" / "mode_contract_matrix.md").read_text(encoding="utf-8")
    matrix = _parse_matrix(md)
    assert "**ASR model default**" in matrix, "matrix parse failed (missing ASR row)"

    base = {
        "diarizer": "auto",
        "speaker_smoothing": False,
        "voice_memory": False,
        "voice_mode": "clone",
        "music_detect": False,
        "separation": "off",
        "mix_mode": "legacy",
        "timing_fit": False,
        "pacing": False,
        "qa": False,
        "director": False,
        "multitrack": False,
    }
    overrides: dict[str, object] = {}
    caps = HardwareCaps(
        gpu_available=gpu,
        has_demucs=True,
        has_whisper=True,
        has_coqui_tts=True,
        has_pyannote=True,
    )

    eff = resolve_effective_settings(mode=mode, base=base, overrides=overrides, caps=caps)
    expected = _expected_from_matrix(matrix, mode=mode, gpu=gpu)

    for k, v in expected.items():
        if v == "as_base":
            assert getattr(eff, k) == base[k]
        else:
            assert getattr(eff, k) == v


@pytest.mark.parametrize("mode", ["high", "medium", "low"])
def test_golden_snapshot_cpu_only(mode: str) -> None:
    """
    Golden snapshots enforce "no drift" in resolver outputs for a CPU-only environment.
    """
    root = _repo_root()
    golden = root / "tests" / "golden" / f"effective_{mode}_cpu.json"
    assert golden.exists(), f"missing golden snapshot: {golden}"
    expected = json.loads(golden.read_text(encoding="utf-8"))

    base = {
        "diarizer": "auto",
        "speaker_smoothing": False,
        "voice_memory": False,
        "voice_mode": "clone",
        "music_detect": False,
        "separation": "off",
        "mix_mode": "legacy",
        "timing_fit": False,
        "pacing": False,
        "qa": False,
        "director": False,
        "multitrack": False,
    }
    caps = HardwareCaps(
        gpu_available=False,
        has_demucs=False,
        has_whisper=False,
        has_coqui_tts=False,
        has_pyannote=False,
    )
    eff = resolve_effective_settings(mode=mode, base=base, overrides={}, caps=caps)
    assert eff.to_dict() == expected
