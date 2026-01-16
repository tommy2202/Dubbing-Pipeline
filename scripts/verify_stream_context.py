from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from dubbing_pipeline.qa.scoring import score_job
from dubbing_pipeline.streaming.context import StreamContextBuffer
from dubbing_pipeline.utils.io import write_json


def _tmp_dir() -> Path:
    return Path("/workspace/_tmp_stream_context").resolve()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_context_buffer_dedup() -> None:
    buf = StreamContextBuffer(context_seconds=15.0)

    prev_src = [
        {"start": 8.0, "end": 10.0, "speaker": "SPEAKER_01", "text": "Hello there."},
        {"start": 10.0, "end": 12.0, "speaker": "SPEAKER_01", "text": "How are you?"},
    ]
    prev_tgt = [
        {"start": 8.0, "end": 10.0, "speaker": "SPEAKER_01", "text": "Hello there."},
        {"start": 10.0, "end": 12.0, "speaker": "SPEAKER_01", "text": "How are you?"},
    ]
    buf.add_translated_segments(chunk_start_s=0.0, src_segments=prev_src, translated_segments=prev_tgt)
    hint = buf.build_translation_hint()
    _assert("Hello there." in hint and "How are you?" in hint, "hint should contain previous translated text")

    # Next chunk begins at 10s with 2s overlap; "How are you?" should be dropped.
    cur_src = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_01", "text": "How are you?"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01", "text": "I'm fine."},
    ]
    kept, rep = buf.dedup_src_segments(chunk_start_s=10.0, src_segments=cur_src, overlap_window_s=2.0)
    _assert(rep.dropped == 1, f"expected 1 dropped, got {rep.dropped}")
    _assert(len(kept) == 1 and kept[0]["text"] == "I'm fine.", "dedup should keep only the new segment")


def test_qa_boundary_checks() -> None:
    root = _tmp_dir()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    stream_dir = root / "stream"
    stream_dir.mkdir(parents=True, exist_ok=True)

    # Create chunk folders with translated.json that will trigger boundary duplicate.
    c0 = stream_dir / "chunk_000"
    c1 = stream_dir / "chunk_001"
    c0.mkdir(parents=True, exist_ok=True)
    c1.mkdir(parents=True, exist_ok=True)

    write_json(
        c0 / "translated.json",
        {
            "src_lang": "ja",
            "tgt_lang": "en",
            "segments": [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_01", "text": "Line A"},
                {"start": 9.0, "end": 10.0, "speaker": "SPEAKER_01", "text": "Boundary line"},
            ],
        },
    )
    write_json(
        c1 / "translated.json",
        {
            "src_lang": "ja",
            "tgt_lang": "en",
            "segments": [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_01", "text": "Boundary line"},
                {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01", "text": "Line B"},
            ],
        },
    )

    manifest = {
        "version": 1,
        "chunk_seconds": 10.0,
        "overlap_seconds": 1.0,
        "context_seconds": 15.0,
        "chunks": [
            {"idx": 0, "start_s": 0.0, "end_s": 10.0, "translated_json": str(c0 / "translated.json")},
            {"idx": 1, "start_s": 9.0, "end_s": 19.0, "translated_json": str(c1 / "translated.json")},
        ],
    }
    (stream_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    summ = score_job(root, enabled=True, write_outputs=False, top_n=50, fail_only=False)
    top = summ.get("top_issues") if isinstance(summ, dict) else None
    _assert(isinstance(top, list), "qa summary must include top_issues list")
    found = any(isinstance(x, dict) and x.get("check_id") == "stream_boundary_duplicate" for x in top)
    _assert(found, "expected stream_boundary_duplicate to appear in QA top issues for streaming boundary repeat")


def main() -> int:
    try:
        test_context_buffer_dedup()
        test_qa_boundary_checks()
    except Exception as ex:
        print(f"verify_stream_context: FAIL: {ex}", file=sys.stderr)
        return 2
    print("verify_stream_context: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

