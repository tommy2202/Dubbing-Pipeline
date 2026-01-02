from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    from anime_v2.review.overrides import (
        apply_music_region_overrides,
        apply_overrides,
        apply_speaker_overrides_to_segments,
        load_overrides,
        save_overrides,
    )

    with tempfile.TemporaryDirectory(prefix="verify_overrides_") as td:
        job = Path(td) / "Output" / "job_x"
        (job / "analysis").mkdir(parents=True, exist_ok=True)

        # Base detected regions
        _write_json(
            job / "analysis" / "music_regions.json",
            {
                "version": 1,
                "regions": [
                    {"start": 0.0, "end": 10.0, "kind": "music", "confidence": 0.9, "reason": "detector"},
                    {"start": 50.0, "end": 60.0, "kind": "singing", "confidence": 0.8, "reason": "detector"},
                ],
            },
        )

        # Overrides: remove first region, edit second, add third
        ov = load_overrides(job)
        ov["music_regions_overrides"] = {
            "adds": [{"start": 100.0, "end": 120.0, "kind": "music", "confidence": 1.0, "reason": "manual"}],
            "removes": [{"start": 0.0, "end": 10.0, "reason": "not_music"}],
            "edits": [
                {
                    "from": {"start": 50.0, "end": 60.0},
                    "to": {"start": 49.5, "end": 61.0, "kind": "music", "confidence": 1.0, "reason": "adjust"},
                }
            ],
        }
        ov["speaker_overrides"] = {"2": "CHAR_A"}
        save_overrides(job, ov)

        base_regs = [
            {"start": 0.0, "end": 10.0, "kind": "music", "confidence": 0.9, "reason": "detector"},
            {"start": 50.0, "end": 60.0, "kind": "singing", "confidence": 0.8, "reason": "detector"},
        ]
        eff = apply_music_region_overrides(base_regs, ov)
        assert all(not (abs(r["start"] - 0.0) < 1e-6 and abs(r["end"] - 10.0) < 1e-6) for r in eff)
        assert any(abs(r["start"] - 49.5) < 1e-6 and abs(r["end"] - 61.0) < 1e-6 for r in eff)
        assert any(abs(r["start"] - 100.0) < 1e-6 and abs(r["end"] - 120.0) < 1e-6 for r in eff)

        # Speaker overrides on segments
        segs = [
            {"segment_id": 1, "speaker": "S1", "text": "a"},
            {"segment_id": 2, "speaker": "S2", "text": "b"},
        ]
        segs2, changed = apply_speaker_overrides_to_segments(segs, ov["speaker_overrides"])
        assert changed == 1
        assert segs2[1]["speaker"] == "CHAR_A"

        # Apply writes effective artifacts + manifest + jsonl log
        rep = apply_overrides(job)
        assert (job / "analysis" / "music_regions.effective.json").exists()
        assert (job / "manifests" / "overrides.json").exists()
        assert (job / "analysis" / "overrides_applied.jsonl").exists()
        assert rep.overrides_hash

        print("verify_overrides: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

