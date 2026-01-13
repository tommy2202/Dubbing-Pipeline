from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dubbing_pipeline.api.models import AuthStore, Role, User, now_ts
from dubbing_pipeline.api.routes_auth import router as auth_router
from dubbing_pipeline.jobs.queue import JobQueue
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.utils.crypto import PasswordHasher
from dubbing_pipeline.web.routes_jobs import router as jobs_router


@dataclass
class _FakeScheduler:
    def submit(self, *_args, **_kwargs) -> None:  # noqa: D401
        # For this verification we only validate submission/cancel wiring, not full processing.
        return


def _make_dummy_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 1s tiny test video with silent audio
    subprocess.run(  # nosec B603
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            "1.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        os.environ["APP_ROOT"] = str(root)
        os.environ["DUBBING_OUTPUT_DIR"] = str((root / "Output").resolve())
        os.environ["DUBBING_LOG_DIR"] = str((root / "logs").resolve())
        os.environ["REMOTE_ACCESS_MODE"] = "off"

        # Create a server-local input file (fallback mode)
        in_dir = root / "Input"
        in_dir.mkdir(parents=True, exist_ok=True)
        src_mp4 = in_dir / "tiny.mp4"
        _make_dummy_mp4(src_mp4)

        app = FastAPI()
        out_root = Path(os.environ["DUBBING_OUTPUT_DIR"]).resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        state_root = (out_root / "_state").resolve()
        state_root.mkdir(parents=True, exist_ok=True)

        # state wiring similar to server lifespan
        app.state.auth_store = AuthStore(state_root / "auth.db")
        store = JobStore(state_root / "jobs.db")
        app.state.job_store = store
        app.state.job_queue = JobQueue(store, concurrency=1)
        app.state.scheduler = _FakeScheduler()

        # bootstrap an operator
        ph = PasswordHasher()
        u = User(
            id="u_test",
            username="alice",
            password_hash=ph.hash("password123"),
            role=Role.operator,
            totp_secret=None,
            totp_enabled=False,
            created_at=now_ts(),
        )
        app.state.auth_store.upsert_user(u)

        app.include_router(auth_router)
        app.include_router(jobs_router)

        with TestClient(app) as c:
            # login
            r = c.post("/auth/login", json={"username": "alice", "password": "password123", "session": True})
            assert r.status_code == 200, r.text
            csrf = c.cookies.get("csrf") or ""
            assert csrf

            # chunked upload
            data = src_mp4.read_bytes()
            init = c.post(
                "/api/uploads/init",
                json={"filename": "tiny.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                headers={"X-CSRF-Token": csrf},
            )
            assert init.status_code == 200, init.text
            upload_id = init.json()["upload_id"]
            chunk_bytes = int(init.json().get("chunk_bytes") or 262144)

            off = 0
            idx = 0
            while off < len(data):
                end = min(len(data), off + chunk_bytes)
                chunk = data[off:end]
                rr = c.post(
                    f"/api/uploads/{upload_id}/chunk?index={idx}&offset={off}",
                    content=chunk,
                    headers={
                        "content-type": "application/octet-stream",
                        "X-Chunk-Sha256": _sha256_hex(chunk),
                        "X-CSRF-Token": csrf,
                    },
                )
                assert rr.status_code == 200, rr.text
                off = end
                idx += 1

            done = c.post(
                f"/api/uploads/{upload_id}/complete",
                json={},
                headers={"X-CSRF-Token": csrf},
            )
            assert done.status_code == 200, done.text
            assert "video_path" in done.json()

            # create job referencing upload_id
            jobr = c.post(
                "/api/jobs",
                json={
                    "upload_id": upload_id,
                    "mode": "low",
                    "device": "cpu",
                    "src_lang": "auto",
                    "tgt_lang": "en",
                    "pg": "off",
                    "qa": False,
                    "cache_policy": "full",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert jobr.status_code == 200, jobr.text
            job_id = jobr.json()["id"]

            # poll detail (should exist and be queued)
            j = c.get(f"/api/jobs/{job_id}")
            assert j.status_code == 200, j.text
            assert j.json().get("state") in {"QUEUED", "RUNNING", "PAUSED"}

            # cancel
            cc = c.post(f"/api/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
            assert cc.status_code == 200, cc.text
            assert cc.json().get("state") == "CANCELED"

            # outputs alias should work (even if empty)
            out = c.get(f"/api/jobs/{job_id}/outputs")
            assert out.status_code == 200, out.text

            # logs alias should work
            lg = c.get(f"/api/jobs/{job_id}/logs?n=10")
            assert lg.status_code == 200

        print("verify_job_submission: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

