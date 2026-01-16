from __future__ import annotations

import dubbing_pipeline.stages.translate as tr


def test_translate_preserves_mapping_and_punctuation(monkeypatch):
    # Fake pipeline returns translations that drop terminal punctuation.
    def fake_pipe(texts):
        return [{"translation_text": "Hello"} for _ in texts]

    monkeypatch.setattr(tr, "_try_make_pipeline", lambda *args, **kwargs: fake_pipe)

    lines = [
        {"start": 0.0, "end": 1.0, "speaker_id": "Speaker1", "text": "こんにちは!"},
        {"start": 1.0, "end": 2.0, "speaker_id": "Speaker2", "text": "元気ですか?"},
    ]

    out = tr.translate_lines(lines, src_lang="ja", tgt_lang="en")
    assert len(out) == 2
    assert out[0]["start"] == 0.0 and out[0]["end"] == 1.0 and out[0]["speaker_id"] == "Speaker1"
    assert out[1]["start"] == 1.0 and out[1]["end"] == 2.0 and out[1]["speaker_id"] == "Speaker2"

    # Terminal punctuation should be preserved even if model dropped it.
    assert out[0]["text"].endswith("!")
    assert out[1]["text"].endswith("?")


def test_translate_falls_back_to_original_text_when_empty(monkeypatch):
    def fake_pipe(texts):
        # Return empty translation for a non-empty source line
        return [{"translation_text": ""} for _ in texts]

    monkeypatch.setattr(tr, "_try_make_pipeline", lambda *args, **kwargs: fake_pipe)

    lines = [{"start": 0.0, "end": 1.0, "speaker_id": "Speaker1", "text": "Bonjour."}]
    out = tr.translate_lines(lines, src_lang="fr", tgt_lang="en")
    assert out[0]["text"] == "Bonjour."
