#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _wav_duration_s(p: Path) -> float | None:
    try:
        import wave

        with wave.open(str(p), "rb") as wf:
            n = wf.getnframes()
            sr = wf.getframerate()
        return float(n) / float(sr) if sr else None
    except Exception:
        return None


def main() -> int:
    """
    Voice fine-tuning / enrollment helper (F9).

    This repository is offline-first and does not ship heavyweight training deps by default.
    Instead of pretending to "train a model", this script **builds a validated dataset manifest**
    that downstream training integrations (external or future) can consume.

    Expected inputs:
      - A folder of audio clips (wav) + transcripts (txt/json) per speaker.
      - A base TTS model identifier (e.g. Coqui XTTS) or checkpoint path.

    Expected outputs:
      - A deterministic `manifest.jsonl` describing samples (paths + text + durations)
      - A `summary.json` to quickly sanity-check counts and missing transcripts
    """
    ap = argparse.ArgumentParser(description="Voice fine-tuning hook (optional).")
    ap.add_argument("--data", type=Path, required=True, help="Training data directory")
    ap.add_argument("--out", type=Path, required=True, help="Output directory for artifacts")
    ap.add_argument("--base-model", default="xtts_v2", help="Base model identifier")
    ap.add_argument(
        "--speaker-id",
        default="speaker",
        help="Logical speaker/character id to associate with this dataset",
    )
    args = ap.parse_args()

    if not args.data.exists():
        print(f"Data path not found: {args.data}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)

    wavs = sorted([p for p in args.data.rglob("*.wav") if p.is_file()])
    if not wavs:
        print(f"No WAV files found under: {args.data}", file=sys.stderr)
        return 2

    manifest_path = args.out / "manifest.jsonl"
    missing_txt: list[str] = []
    total_dur = 0.0
    rows = 0

    with manifest_path.open("w", encoding="utf-8") as f:
        for wav in wavs:
            txt = wav.with_suffix(".txt")
            js = wav.with_suffix(".json")

            text: str | None = None
            meta: dict[str, Any] = {}
            if txt.exists():
                text = txt.read_text(encoding="utf-8", errors="replace").strip()
            elif js.exists():
                try:
                    data = json.loads(js.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        text = str(data.get("text") or "").strip() or None
                        meta = data if data else {}
                except Exception:
                    text = None

            if not text:
                missing_txt.append(str(wav))
                continue

            dur = _wav_duration_s(wav)
            if dur is not None:
                total_dur += float(dur)

            rec = {
                "speaker_id": str(args.speaker_id),
                "base_model": str(args.base_model),
                "wav": str(wav.resolve()),
                "text": text,
                "duration_s": float(dur) if dur is not None else None,
                "meta": meta or None,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rows += 1

    summary = {
        "speaker_id": str(args.speaker_id),
        "base_model": str(args.base_model),
        "input_dir": str(args.data.resolve()),
        "output_dir": str(args.out.resolve()),
        "wav_files": len(wavs),
        "samples_with_text": rows,
        "missing_transcript": len(missing_txt),
        "total_duration_s": round(float(total_dur), 3),
        "missing_transcript_examples": missing_txt[:10],
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    (args.out / "README.txt").write_text(
        "This directory contains a dataset manifest for external/future voice fine-tuning.\n"
        "\n"
        "Files:\n"
        "- manifest.jsonl: one sample per line (wav path + text + duration)\n"
        "- summary.json: counts + quick sanity stats\n",
        encoding="utf-8",
    )

    if rows == 0:
        print("No usable samples (missing transcripts for all WAVs).", file=sys.stderr)
        return 2

    if missing_txt:
        print(
            f"WARN: {len(missing_txt)} wav files missing transcript (.txt/.json); skipped",
            file=sys.stderr,
        )

    print(f"OK: wrote {rows} samples → {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
