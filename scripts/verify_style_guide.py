from __future__ import annotations

import json
import sys
from pathlib import Path

from config.settings import get_safe_config_report


def main() -> int:
    print("safe_config_report:", get_safe_config_report())
    from dubbing_pipeline.text.style_guide import apply_style_guide, load_style_guide

    tmp = Path("_tmp_style_guide")
    tmp.mkdir(parents=True, exist_ok=True)
    guide_path = tmp / "style_guide.json"
    guide_path.write_text(
        json.dumps(
            {
                "version": 1,
                "project": "testproj",
                "name_map": {"Tanjiro": "Tanjiro Kamado"},
                "glossary_terms": [
                    {"source": "鬼殺隊", "target": "Demon Slayer Corps", "case_sensitive": False}
                ],
                "honorific_policy": {"keep": True, "map": {"-san": ""}},
                "phrase_rules": [
                    {"id": "tighten", "order": 1, "stage": "post_translate", "pattern": "\\bin order to\\b", "replace": "to", "flags": "i"},
                    # conflict pair (toggle) should be detected
                    {"id": "flip1", "order": 2, "stage": "post_translate", "pattern": "\\bfoo\\b", "replace": "bar", "flags": "i"},
                    {"id": "flip2", "order": 3, "stage": "post_translate", "pattern": "\\bbar\\b", "replace": "foo", "flags": "i"},
                ],
                "forbidden_terms": ["forbiddenword"],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    guide = load_style_guide(guide_path, project="testproj")

    inp = "Tanjiro-san joined the 鬼殺隊 in order to foo forbiddenword."
    out1, applied1, meta1 = apply_style_guide(inp, guide, stage="post_translate")
    out2, applied2, meta2 = apply_style_guide(inp, guide, stage="post_translate")
    if out1 != out2 or json.dumps([a.to_dict() for a in applied1], sort_keys=True) != json.dumps(
        [a.to_dict() for a in applied2], sort_keys=True
    ):
        print("ERROR: non-deterministic output", file=sys.stderr)
        return 2

    if "Tanjiro Kamado" not in out1:
        print("ERROR: name_map not applied", file=sys.stderr)
        return 3
    if "Demon Slayer Corps" not in out1:
        print("ERROR: glossary_terms not applied", file=sys.stderr)
        return 4
    if "-san" in out1.lower():
        print("ERROR: honorific map not applied", file=sys.stderr)
        return 5
    if "in order to" in out1.lower():
        print("ERROR: phrase rule not applied", file=sys.stderr)
        return 6
    if not meta1.get("forbidden_hits"):
        print("ERROR: forbidden term not detected", file=sys.stderr)
        return 7
    # conflict should be detected due to flip rules loop
    if not meta1.get("conflict"):
        print("ERROR: expected conflict detection to trigger", file=sys.stderr)
        return 8

    print("OK: verify_style_guide passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

