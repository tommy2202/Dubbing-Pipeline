from __future__ import annotations

import wave
from pathlib import Path

from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.voice_profiles.manager import (
    create_profiles_for_refs,
    match_profiles_for_refs,
    profile_ref_path,
)


def _write_wav(path: Path, *, freq_hz: float = 220.0, duration_s: float = 0.4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sr = 16000
    n = int(sr * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            # simple deterministic tone
            v = int(12000 * __import__("math").sin(2 * __import__("math").pi * freq_hz * i / sr))
            frames += int(v).to_bytes(2, byteorder="little", signed=True)
        wf.writeframes(bytes(frames))


def test_voice_profiles_persist_and_reuse(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)
    voice_store_dir = tmp_path / "voice_store"
    series_slug = "series-1"

    # Episode 1: create profile from ref.
    ref1 = tmp_path / "ep1_speaker_a.wav"
    _write_wav(ref1, freq_hz=220.0, duration_s=0.5)
    created = create_profiles_for_refs(
        store=store,
        series_slug=series_slug,
        label_refs={"SPEAKER_01": ref1},
        created_by="user1",
        source_job_id="job_ep1",
        device="cpu",
        voice_store_dir=voice_store_dir,
    )
    assert "SPEAKER_01" in created
    pid = str(created["SPEAKER_01"]["profile_id"])
    assert pid
    # Ensure ref persisted.
    ref_path = profile_ref_path(pid, voice_store_dir=voice_store_dir)
    assert ref_path.exists()
    prof = store.get_voice_profile(pid)
    assert prof is not None
    assert prof.get("series_lock") == series_slug

    # Episode 2: match same speaker to existing profile.
    ref2 = tmp_path / "ep2_speaker_x.wav"
    _write_wav(ref2, freq_hz=220.0, duration_s=0.5)
    matches = match_profiles_for_refs(
        store=store,
        series_slug=series_slug,
        label_refs={"SPEAKER_99": ref2},
        allow_global=False,
        threshold=0.2,
        device="cpu",
        voice_store_dir=voice_store_dir,
    )
    assert "SPEAKER_99" in matches
    assert matches["SPEAKER_99"]["profile_id"] == pid
