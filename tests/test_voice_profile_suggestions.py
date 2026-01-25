from __future__ import annotations

import os
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.server import app
from dubbing_pipeline.voice_memory.embeddings import compute_embedding
from dubbing_pipeline.voice_profiles.manager import create_profiles_for_refs


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
            v = int(12000 * __import__("math").sin(2 * __import__("math").pi * freq_hz * i / sr))
            frames += int(v).to_bytes(2, byteorder="little", signed=True)
        wf.writeframes(bytes(frames))


def _login_admin(c: TestClient) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    assert r.status_code == 200
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}", "X-CSRF-Token": data["csrf_token"]}


def test_voice_profile_suggestions_accept(tmp_path: Path) -> None:
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "adminpass"
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["VOICE_PROFILE_SUGGEST_THRESHOLD"] = "0.1"
    get_settings.cache_clear()

    with TestClient(app) as c:
        headers = _login_admin(c)
        store = c.app.state.job_store
        auth_store = c.app.state.auth_store
        admin_user = auth_store.get_user_by_username("admin")
        admin_id = str(admin_user.id if admin_user else "admin")

        voice_store_dir = tmp_path / "voice_store"
        ref1 = tmp_path / "ref_global.wav"
        _write_wav(ref1, freq_hz=220.0, duration_s=0.5)
        emb1, provider1 = compute_embedding(ref1, device="cpu")
        assert emb1 is not None
        store.upsert_voice_profile(
            profile_id="vp_global",
            display_name="GlobalVoice",
            created_by=admin_id,
            scope="global",
            series_lock=None,
            source_type="user_upload",
            export_allowed=True,
            share_allowed=True,
            reuse_allowed=1,
            expires_at=None,
            embedding_vector=emb1,
            embedding_model_id=str(provider1 or ""),
            metadata_json={"ref_path": str(ref1)},
        )

        ref2 = tmp_path / "ref_series.wav"
        _write_wav(ref2, freq_hz=220.0, duration_s=0.5)
        created = create_profiles_for_refs(
            store=store,
            series_slug="series-a",
            label_refs={"SPEAKER_01": ref2},
            created_by=admin_id,
            source_job_id="job_ep1",
            device="cpu",
            voice_store_dir=voice_store_dir,
        )
        pid = str(created["SPEAKER_01"]["profile_id"])

        # Suggestions created, but no auto-merge.
        suggestions = store.list_voice_profile_suggestions(pid)
        assert any(s.get("suggested_profile_id") == "vp_global" for s in suggestions)
        assert not store.has_voice_profile_alias(pid, "vp_global")

        r = c.get(f"/api/voices/{pid}/suggestions", headers=headers)
        assert r.status_code == 200
        items = r.json().get("items") or []
        assert items
        sug = next((it for it in items if it.get("suggested_profile_id") == "vp_global"), items[0])
        sug_id = sug["id"]

        r2 = c.post(
            f"/api/voices/{pid}/accept_suggestion",
            headers=headers,
            json={"suggestion_id": sug_id, "action": "use_existing"},
        )
        assert r2.status_code == 200
        alias = store.get_voice_profile_alias(pid, "vp_global")
        assert alias is not None
        assert int(alias.get("approved_by_admin") or 0) == 0
