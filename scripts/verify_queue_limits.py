from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from anime_v2.api.models import AuthStore, Role, User, now_ts
from anime_v2.api.security import create_access_token
from anime_v2.jobs.store import JobStore
from anime_v2.runtime.scheduler import Scheduler
from anime_v2.utils.crypto import PasswordHasher
from anime_v2.utils.ratelimit import RateLimiter
from anime_v2.web.routes_jobs import router as jobs_router


def _make_tiny_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1.0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _user(*, user_id: str, role: Role) -> User:
    ph = PasswordHasher()
    return User(
        id=user_id,
        username=user_id,
        password_hash=ph.hash("pw"),
        role=role,
        totp_secret=None,
        totp_enabled=False,
        created_at=now_ts(),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = (root / "Output").resolve()
        inp = (root / "Input").resolve()
        state = (out / "_state").resolve()
        out.mkdir(parents=True, exist_ok=True)
        inp.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)

        # Policy: only 1 inflight job allowed for non-admin (max_active=1, max_queued=0).
        os.environ["APP_ROOT"] = str(root)
        os.environ["ANIME_V2_OUTPUT_DIR"] = str(out)
        os.environ["REMOTE_ACCESS_MODE"] = "off"
        os.environ["COOKIE_SECURE"] = "0"
        os.environ["ANIME_V2_JWT_SECRET"] = "test-secret-please-change"
        os.environ["ANIME_V2_CSRF_SECRET"] = "test-secret-please-change"
        os.environ["ANIME_V2_SESSION_SECRET"] = "test-secret-please-change"
        os.environ["ANIME_V2_MAX_ACTIVE_JOBS_PER_USER"] = "1"
        os.environ["ANIME_V2_MAX_QUEUED_JOBS_PER_USER"] = "0"

        # Clear cached settings between scripts.
        try:
            from config import settings as cfg_settings

            cfg_settings.get_settings.cache_clear()
        except Exception:
            pass

        # Setup stores
        auth = AuthStore(state / "auth.db")
        alice = _user(user_id="u_alice", role=Role.operator)
        auth.upsert_user(alice)

        store = JobStore(state / "jobs.db")

        # Minimal scheduler (no worker loop). We only test admission.
        sched = Scheduler(store=store, enqueue_cb=lambda job: None)
        Scheduler.install(sched)

        app = FastAPI()
        app.state.auth_store = auth
        app.state.job_store = store
        app.state.scheduler = sched
        app.state.rate_limiter = RateLimiter()
        app.include_router(jobs_router)

        # Create a valid video file under Input/
        vid = inp / "Test.mp4"
        _make_tiny_mp4(vid)

        token = create_access_token(
            sub=alice.id,
            role=str(alice.role.value),
            scopes=["submit:job", "read:job"],
            minutes=60,
        )
        h = {"Authorization": f"Bearer {token}", "content-type": "application/json"}

        payload = {
            "video_path": "Input/Test.mp4",
            "series_title": "My Show",
            "season_text": "S1",
            "episode_text": "E1",
            "mode": "low",
        }

        with TestClient(app) as c:
            r1 = c.post("/api/jobs", headers=h, json=dict(payload))
            assert r1.status_code == 200, r1.text
            jid1 = (r1.json() or {}).get("id")
            assert jid1

            # Second submission should be rejected by inflight cap (since first is queued).
            payload2 = dict(payload)
            payload2["episode_text"] = "E2"
            r2 = c.post("/api/jobs", headers=h, json=payload2)
            assert r2.status_code == 429, r2.text
            detail = (r2.json() or {}).get("detail")
            assert isinstance(detail, str) and "in-flight" in detail.lower(), detail

        # Ensure first job exists and is queued.
        j = store.get(str(jid1))
        assert j is not None
        assert j.state.value in {"QUEUED", "RUNNING"}

    print("verify_queue_limits: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

