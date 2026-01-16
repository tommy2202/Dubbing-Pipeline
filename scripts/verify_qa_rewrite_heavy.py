from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    from dubbing_pipeline.qa.scoring import score_job

    with tempfile.TemporaryDirectory(prefix="verify_qa_rewrite_heavy_") as td:
        job = Path(td) / "Output" / "job_qa"
        job.mkdir(parents=True, exist_ok=True)
        (job / "qa").mkdir(parents=True, exist_ok=True)
        (job / "analysis").mkdir(parents=True, exist_ok=True)

        # translated.json with timing-fit metadata
        segs = [
            {
                "segment_id": 1,
                "start": 0.0,
                "end": 2.0,
                "speaker": "SPEAKER_01",
                "text_pre_fit": "This is a very long line that will be aggressively shortened by timing fit.",
                "text": "This is shortened.",
                "timing_fit": {"passes": 4},
            },
            {
                "segment_id": 2,
                "start": 2.0,
                "end": 3.0,
                "speaker": "SPEAKER_01",
                "text": "ok",
            },
        ]
        _write_json(job / "translated.json", {"src_lang": "ja", "tgt_lang": "en", "segments": segs})

        # tts manifest with pacing hard trim for segment 1
        _write_json(
            job / "tts_manifest.json",
            {
                "clips": [],
                "wav_out": "",
                "lines": [
                    {"start": 0.0, "end": 2.0, "text": "x", "pacing": {"enabled": True, "hard_trim": True, "min_ratio": 0.88, "max_ratio": 1.18, "atempo_ratio": 1.18}},
                    {"start": 2.0, "end": 3.0, "text": "ok"},
                ],
            },
        )

        summary = score_job(job, enabled=True, write_outputs=False, top_n=50, fail_only=False)
        tops = summary.get("top_issues", [])
        assert isinstance(tops, list)
        ids = {it.get("check_id") for it in tops if isinstance(it, dict)}
        assert "rewrite_heavy" in ids, "expected rewrite_heavy check"
        assert "pacing_heavy" in ids, "expected pacing_heavy check"

        # fix link presence
        any_fix = any(isinstance(it, dict) and isinstance(it.get("fix"), dict) and it["fix"].get("ui_url") for it in tops)
        assert any_fix, "expected fix link metadata in top issues"

    print("verify_qa_rewrite_heavy: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

