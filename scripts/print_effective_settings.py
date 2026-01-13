#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.modes import resolve_effective_settings
from dubbing_pipeline.utils.io import write_json


def main() -> int:
    ap = argparse.ArgumentParser(description="Print and persist effective settings for a job config.")
    ap.add_argument("--job-dir", type=Path, required=True, help="Job directory under Output/<job>")
    ap.add_argument("--mode", default="medium", help="Requested mode: high|medium|low")
    ap.add_argument("--device", default="auto", help="Device preference: auto|cpu|cuda")
    ap.add_argument("--asr-model", default="", help="Explicit ASR model override (optional)")
    ap.add_argument("--diarizer", default="", help="Explicit diarizer override (auto|pyannote|speechbrain|heuristic|off)")
    ap.add_argument("--mix-mode", default="", help="Explicit mix mode override (legacy|enhanced)")
    ap.add_argument("--separation", default="", help="Explicit separation override (off|demucs)")
    ap.add_argument("--timing-fit", action="store_true", help="Enable timing-fit (explicit override)")
    ap.add_argument("--pacing", action="store_true", help="Enable pacing (explicit override)")
    ap.add_argument("--qa", action="store_true", help="Enable QA (explicit override)")
    ap.add_argument("--director", action="store_true", help="Enable director mode (explicit override)")
    ap.add_argument("--voice-memory", action="store_true", help="Enable voice memory (explicit override)")
    ap.add_argument("--speaker-smoothing", action="store_true", help="Enable speaker smoothing (explicit override)")
    ap.add_argument("--multitrack", action="store_true", help="Enable multitrack (explicit override)")
    args = ap.parse_args()

    s = get_settings()
    job_dir = Path(args.job_dir).resolve()
    analysis = job_dir / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)

    base = {
        "diarizer": str(args.diarizer or getattr(s, "diarizer", "auto")),
        "speaker_smoothing": bool(getattr(s, "speaker_smoothing", False)),
        "voice_memory": bool(getattr(s, "voice_memory", False)),
        "voice_mode": str(getattr(s, "voice_mode", "clone")),
        "music_detect": bool(getattr(s, "music_detect", False)),
        "separation": str(args.separation or getattr(s, "separation", "off")),
        "mix_mode": str(args.mix_mode or getattr(s, "mix_mode", "legacy")),
        "timing_fit": bool(getattr(s, "timing_fit", False)),
        "pacing": bool(getattr(s, "pacing", False)),
        "qa": False,
        "director": bool(getattr(s, "director", False)),
        "multitrack": bool(getattr(s, "multitrack", False)),
    }

    overrides = {}
    if args.asr_model.strip():
        overrides["asr_model"] = args.asr_model.strip()
    if args.diarizer.strip():
        overrides["diarizer"] = args.diarizer.strip()
    if args.mix_mode.strip():
        overrides["mix_mode"] = args.mix_mode.strip()
    if args.separation.strip():
        overrides["separation"] = args.separation.strip()
    if args.timing_fit:
        overrides["timing_fit"] = True
    if args.pacing:
        overrides["pacing"] = True
    if args.qa:
        overrides["qa"] = True
    if args.director:
        overrides["director"] = True
    if args.voice_memory:
        overrides["voice_memory"] = True
    if args.speaker_smoothing:
        overrides["speaker_smoothing"] = True
    if args.multitrack:
        overrides["multitrack"] = True

    eff = resolve_effective_settings(mode=str(args.mode), base=base, overrides=overrides)
    payload = eff.to_dict()
    payload["job_dir"] = str(job_dir)
    payload["device_pref"] = str(args.device)

    outp = analysis / "effective_settings.json"
    write_json(outp, payload)

    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nWROTE: {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

