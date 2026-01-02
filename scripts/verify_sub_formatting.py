from __future__ import annotations

import tempfile
from pathlib import Path


def main() -> int:
    from anime_v2.subs.formatting import SubtitleFormatRules, format_subtitle_blocks_with_stats, write_formatted_subs_variant

    rules = SubtitleFormatRules(max_chars_per_line=20, max_lines=2, max_cps=25.0, min_duration_s=0.8, max_duration_s=7.0)

    blocks = [
        {"start": 0.0, "end": 0.3, "text": "This is a very long subtitle line that should be wrapped nicely."},
        {"start": 1.0, "end": 2.0, "text": "Short."},
        {"start": 2.1, "end": 2.6, "text": "OneTwoThreeFourFiveSixSevenEightNineTenElevenTwelve"},
    ]

    formatted, stats = format_subtitle_blocks_with_stats(blocks, rules)
    assert stats.blocks == 3
    assert stats.changed_blocks >= 1
    # duration extension should happen on block 1 (0.3s -> >=0.8s, but not past next start)
    assert formatted[0]["end"] - formatted[0]["start"] >= 0.0

    for b in formatted:
        txt = str(b.get("text") or "")
        lines = txt.split("\n")
        assert len(lines) <= rules.max_lines
        assert all(len(ln) <= rules.max_chars_per_line for ln in lines)

    with tempfile.TemporaryDirectory(prefix="verify_sub_formatting_") as td:
        job = Path(td) / "Output" / "job_subs"
        row = write_formatted_subs_variant(job_dir=job, variant="tgt_literal", blocks=blocks, project=None)
        assert (job / "subs" / "tgt_literal.srt").exists()
        assert (job / "subs" / "tgt_literal.vtt").exists()
        assert (job / "analysis" / "subs_formatting_summary.json").exists()
        assert isinstance(row, dict)

    print("verify_sub_formatting: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

