from __future__ import annotations

import json
import math
import wave
from pathlib import Path

from config.settings import get_safe_config_report


def _write_wav_pcm16(path: Path, *, seconds: float, sr: int = 16000, amp: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(seconds * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n):
            s = math.sin(2.0 * math.pi * 220.0 * (i / sr))
            v = int(max(-1.0, min(1.0, amp * s)) * 32767.0)
            wf.writeframesraw(int(v).to_bytes(2, "little", signed=True))


def _write_clipped_wav(path: Path, *, seconds: float, sr: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(seconds * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for _ in range(n):
            wf.writeframesraw(int(32767).to_bytes(2, "little", signed=True))


def main() -> int:
    print("safe_config_report:", get_safe_config_report())
    from anime_v2.qa.scoring import score_job

    job = Path("_tmp_qa_job")
    if job.exists():
        # keep it simple; overwrite key files
        pass
    job.mkdir(parents=True, exist_ok=True)

    # translated segments
    segs = [
        # drift + overlap: audio 1.6s in 1.0s window
        {"segment_id": 1, "start": 0.0, "end": 1.0, "speaker": "A", "text": "hello world", "conf": -0.2},
        # high speaking rate: many words in 1s
        {
            "segment_id": 2,
            "start": 1.0,
            "end": 2.0,
            "speaker": "B",
            "text": "one two three four five six seven eight nine ten eleven",
            "conf": -0.2,
        },
        # clipping
        {"segment_id": 3, "start": 2.0, "end": 3.0, "speaker": "A", "text": "ok", "conf": -0.2},
        # low confidence
        {"segment_id": 4, "start": 3.0, "end": 4.0, "speaker": "B", "text": "??", "conf": -1.3},
    ]
    (job / "translated.json").write_text(
        json.dumps({"src_lang": "ja", "tgt_lang": "en", "segments": segs}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # music regions overlapping seg2 (info)
    (job / "analysis").mkdir(parents=True, exist_ok=True)
    (job / "analysis" / "music_regions.json").write_text(
        json.dumps({"version": 1, "regions": [{"start": 1.0, "end": 2.0, "kind": "music", "confidence": 0.9}]}, indent=2),
        encoding="utf-8",
    )

    # review state (lock seg1 so suggestion considers locked)
    (job / "review").mkdir(parents=True, exist_ok=True)
    (job / "review" / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job": {},
                "segments": [
                    {
                        "segment_id": 1,
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": "A",
                        "chosen_text": "hello world",
                        "audio_path_current": str(job / "review" / "audio" / "1_v1.wav"),
                        "status": "locked",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    # tts clips referenced via tts_manifest.json (fallback for other segments)
    clips_dir = job / "tts_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    _write_wav_pcm16(job / "review" / "audio" / "1_v1.wav", seconds=1.6, amp=0.2)
    _write_wav_pcm16(clips_dir / "0001_B.wav", seconds=1.0, amp=0.2)
    _write_clipped_wav(clips_dir / "0002_A.wav", seconds=1.0)
    _write_wav_pcm16(clips_dir / "0003_B.wav", seconds=1.0, amp=0.2)
    (job / "tts_manifest.json").write_text(
        json.dumps(
            {
                "clips": [
                    str(job / "review" / "audio" / "1_v1.wav"),
                    str(clips_dir / "0001_B.wav"),
                    str(clips_dir / "0002_A.wav"),
                    str(clips_dir / "0003_B.wav"),
                ],
                "lines": segs,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    summary = score_job(job, enabled=True, write_outputs=True, top_n=50, fail_only=False)
    qa_dir = job / "qa"
    assert (qa_dir / "summary.json").exists()
    assert (qa_dir / "segment_scores.jsonl").exists()
    assert (qa_dir / "top_issues.md").exists()

    # Ensure expected checks appear
    top_ids = {it.get("check_id") for it in (summary.get("top_issues") or []) if isinstance(it, dict)}
    needed = {"alignment_drift", "segment_overlap", "speaking_rate", "audio_clipping", "low_asr_confidence"}
    if not needed.intersection(top_ids):
        raise AssertionError(f"Expected some check_ids in top issues, got: {top_ids}")

    print("OK: verify_qa passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

