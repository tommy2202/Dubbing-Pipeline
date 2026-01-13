#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from dubbing_pipeline.timing.fit_text import (
        estimate_speaking_seconds,
        fit_translation_to_time,
        shorten_english,
    )

    samples = [
        "Well, I do not really want to do that, basically.",
        "In order to make sure that it is not too long, we should shorten it.",
        "Um, this is actually kind of really, really important!",
    ]
    for s in samples:
        short = shorten_english(s)
        est0 = estimate_speaking_seconds(s)
        est1 = estimate_speaking_seconds(short)
        fitted, stats = fit_translation_to_time(
            s, target_seconds=1.5, tolerance=0.10, wps=2.7, max_passes=4
        )
        print("---")
        print("orig:", s)
        print("short:", short)
        print("est_before_s:", round(est0, 3), "est_after_s:", round(est1, 3))
        print("fitted:", fitted)
        print("stats:", stats.to_dict())

    # Ensure atempo chain helper doesn't crash
    from dubbing_pipeline.timing.pacing import compute_ratio

    assert compute_ratio(2.0, 1.0) > 1.0
    assert compute_ratio(1.0, 2.0) < 1.0

    print("VERIFY_TIMING_FIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
