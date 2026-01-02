from __future__ import annotations

import json
import sys
from pathlib import Path

from config.settings import get_safe_config_report


def main() -> int:
    print("safe_config_report:", get_safe_config_report())

    from anime_v2.text.pg_filter import apply_pg_filter, apply_pg_filter_to_segments, built_in_policy

    samples = [
        ("pg13", "This is fucking shit.", "This is freaking crap."),
        ("pg", "This is fucking shit.", "This is freaking crud."),
    ]
    for pol_name, inp, expected in samples:
        pol = built_in_policy(pol_name)
        out1, trig1 = apply_pg_filter(inp, pol)
        out2, trig2 = apply_pg_filter(inp, pol)
        if out1 != out2 or json.dumps([t.to_dict() for t in trig1], sort_keys=True) != json.dumps(
            [t.to_dict() for t in trig2], sort_keys=True
        ):
            print("ERROR: non-deterministic output for policy", pol_name, file=sys.stderr)
            return 2
        if out1 != expected:
            print("ERROR: unexpected output for policy", pol_name, "got:", out1, file=sys.stderr)
            return 3
        # Ensure triggers do not include raw term
        dump = json.dumps([t.to_dict() for t in trig1], sort_keys=True).lower()
        if "fuck" in dump or "shit" in dump:
            print("ERROR: trigger dump leaked raw term", file=sys.stderr)
            return 4

    tmp = Path("_tmp_pg_filter")
    tmp.mkdir(parents=True, exist_ok=True)
    report_path = tmp / "pg_filter_report.json"

    segs = [
        {"segment_id": 1, "start": 0.0, "end": 1.0, "text": "hello there"},
        {"segment_id": 2, "start": 1.0, "end": 2.0, "text": "this is fucking shit"},
    ]
    out, report = apply_pg_filter_to_segments(
        segs,
        pg="pg13",
        pg_policy_path=None,
        report_path=report_path,
        job_id="verify_pg",
    )
    if not report_path.exists():
        print("ERROR: report not written", file=sys.stderr)
        return 5
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "segments" not in payload:
        print("ERROR: invalid report JSON", file=sys.stderr)
        return 6
    report_text = report_path.read_text(encoding="utf-8").lower()
    if "fuck" in report_text or "shit" in report_text:
        print("ERROR: report leaked raw term", file=sys.stderr)
        return 7
    if out[1]["text"] == segs[1]["text"]:
        print("ERROR: expected segment 2 to be sanitized", file=sys.stderr)
        return 8

    print("OK: verify_pg_filter passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

