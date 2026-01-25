from __future__ import annotations

from dubbing_pipeline.text.glossary import (
    apply_glossary_to_segments,
    apply_glossary_to_text,
    build_rules_from_glossaries,
    parse_tsv_glossary,
)
from dubbing_pipeline.text.pronunciation import apply_pronunciation, normalize_pronunciations


def test_glossary_deterministic_order() -> None:
    glossaries = [
        {
            "id": "g_low",
            "name": "Generic",
            "priority": 1,
            "rules_json": {
                "rules": [
                    {"kind": "exact", "source": "Rimuru", "target": "Rimuru Tempest"},
                ]
            },
        },
        {
            "id": "g_high",
            "name": "Series",
            "priority": 10,
            "rules_json": {
                "rules": [
                    {"kind": "regex", "pattern": r"\bLord\b", "replace": "Lord (title)"},
                ]
            },
        },
    ]
    rules = build_rules_from_glossaries(glossaries)
    out, applied = apply_glossary_to_text("Lord Rimuru", rules)
    assert out == "Lord (title) Rimuru Tempest"
    assert applied
    assert applied[0]["rule_id"].startswith("g_high")


def test_glossary_fallback_without_llm() -> None:
    rules = parse_tsv_glossary(["Slime\tSlime Lord"])
    segs = [{"text": "Slime"}]
    out = apply_glossary_to_segments(segs, rules)
    assert out[0]["text"] == "Slime Lord"
    assert out[0]["text_post_glossary"] == "Slime Lord"
    assert out[0]["text_pre_glossary"] == "Slime"


def test_pronunciation_fallback_to_spelling_hints() -> None:
    entries = normalize_pronunciations(
        [
            {
                "term": "Kobayashi",
                "ipa_or_phoneme": {"format": "ipa", "value": "ko-ba-ya-shi"},
            }
        ]
    )
    out, warnings = apply_pronunciation("Kobayashi is here", entries, provider="xtts")
    assert "ko-ba-ya-shi" in out
    assert warnings
