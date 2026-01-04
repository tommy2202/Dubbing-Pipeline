from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _reset_settings_env(env: dict[str, str]) -> None:
    for k in list(env.keys()):
        os.environ.pop(k, None)
    os.environ.update(env)
    # settings is an lru_cache; clear between scenarios
    try:
        from config.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass


async def _asgi_get(
    app, *, path: str, client_ip: str, headers: list[tuple[bytes, bytes]] | None = None
) -> int:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii", errors="ignore"),
        "query_string": b"",
        "headers": headers or [],
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next((m for m in messages if m.get("type") == "http.response.start"), None)
    if not start:
        raise RuntimeError("no_response_start")
    return int(start.get("status") or 0)


def _verify_remote_modes() -> None:
    """
    Confirms REMOTE_ACCESS_MODE enforcement decisions (off/tailscale/cloudflare).
    """
    from anime_v2.api.remote_access import remote_access_middleware

    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    app.middleware("http")(remote_access_middleware)

    async def _run():
        # off: allow all
        _reset_settings_env({"REMOTE_ACCESS_MODE": "off"})
        ok0 = await _asgi_get(app, path="/healthz", client_ip="8.8.8.8")
        assert ok0 == 200, f"off should allow, got {ok0}"

        # tailscale: allow 100.64/10 and block public
        _reset_settings_env({"REMOTE_ACCESS_MODE": "tailscale"})
        ok1 = await _asgi_get(app, path="/healthz", client_ip="100.100.100.100")
        bad1 = await _asgi_get(app, path="/healthz", client_ip="8.8.8.8")
        assert ok1 == 200, f"tailscale allowed IP should be 200, got {ok1}"
        assert bad1 == 403, f"tailscale public IP should be 403, got {bad1}"

        # cloudflare: default allowlist is private/loopback-ish; block public
        _reset_settings_env({"REMOTE_ACCESS_MODE": "cloudflare"})
        ok2 = await _asgi_get(app, path="/healthz", client_ip="172.16.0.10")
        bad2 = await _asgi_get(app, path="/healthz", client_ip="8.8.8.8")
        assert ok2 == 200, f"cloudflare private peer should be 200, got {ok2}"
        assert bad2 == 403, f"cloudflare public peer should be 403, got {bad2}"

        # cloudflare + trusted proxy headers: accept proxied client IP from a trusted peer
        _reset_settings_env(
            {
                "REMOTE_ACCESS_MODE": "cloudflare",
                "TRUST_PROXY_HEADERS": "1",
                "ALLOWED_SUBNETS": "127.0.0.0/8",
                "TRUSTED_PROXY_SUBNETS": "127.0.0.0/8",
            }
        )
        ok3 = await _asgi_get(
            app,
            path="/healthz",
            client_ip="127.0.0.1",
            headers=[(b"x-forwarded-for", b"100.99.88.77")],
        )
        assert ok3 == 200, f"cloudflare proxied request should be 200, got {ok3}"

    asyncio.run(_run())


def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603


def _make_synthetic_mp4(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )


def _write_minimal_translated_json(job_dir: Path) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "src_lang": "ja",
        "tgt_lang": "en",
        "segments": [
            {
                "segment_id": 1,
                "start": 0.0,
                "end": 1.0,
                "speaker": "SPEAKER_01",
                "src_text": "こんにちは",
                "text": "Hello.",
                "text_pre_fit": "Hello.",
            }
        ],
    }
    (job_dir / "translated.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _verify_end_to_end() -> None:
    """
    Runs the canonical server in-process and verifies:
    - auth session login (no legacy token)
    - chunked upload + job submission
    - job queue progresses to DONE
    - mobile MP4 exists and supports Range
    - QA artifacts exist and are fetchable
    - review endpoints work (edit/helper/regen/audio/lock)
    - logs endpoint works
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        app_root = root
        out_dir = (root / "Output").resolve()
        log_dir = (root / "logs").resolve()
        in_dir = (root / "Input").resolve()
        uploads_dir = (in_dir / "uploads").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        in_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # synthetic input file
        src_mp4 = in_dir / "tiny.mp4"
        _make_synthetic_mp4(src_mp4)

        # Configure env BEFORE importing the app/settings.
        os.environ["APP_ROOT"] = str(app_root)
        os.environ["ANIME_V2_OUTPUT_DIR"] = str(out_dir)
        os.environ["ANIME_V2_LOG_DIR"] = str(log_dir)
        os.environ["INPUT_DIR"] = str(in_dir)
        os.environ["INPUT_UPLOADS_DIR"] = str(uploads_dir)
        os.environ["REMOTE_ACCESS_MODE"] = "off"
        # Ensure proxy headers are NOT trusted in off mode (proxy-safe default)
        os.environ["TRUST_PROXY_HEADERS"] = "0"
        os.environ.pop("ALLOWED_SUBNETS", None)
        os.environ.pop("TRUSTED_PROXY_SUBNETS", None)
        os.environ.pop("CLOUDFLARE_ACCESS_TEAM_DOMAIN", None)
        os.environ.pop("CLOUDFLARE_ACCESS_AUD", None)
        os.environ["ALLOW_LEGACY_TOKEN_LOGIN"] = "0"
        # keep secure cookies off for http TestClient
        os.environ["COOKIE_SECURE"] = "0"
        # bootstrap an admin
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "password123"
        # avoid accidental egress/downloads during tests
        os.environ["OFFLINE_MODE"] = "1"
        os.environ["ALLOW_EGRESS"] = "0"

        try:
            from config.settings import get_settings

            get_settings.cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass

        from anime_v2.server import app

        with TestClient(app) as c:
            # 1) login via username/password (session cookie)
            r = c.post("/auth/login", json={"username": "admin", "password": "password123", "session": True})
            assert r.status_code == 200, r.text
            assert c.cookies.get("session"), "missing session cookie"
            csrf = c.cookies.get("csrf") or ""
            assert csrf, "missing csrf cookie"

            # 2) chunked upload
            data = src_mp4.read_bytes()
            init = c.post(
                "/api/uploads/init",
                json={"filename": "tiny.mp4", "total_bytes": len(data), "mime": "video/mp4"},
                headers={"X-CSRF-Token": csrf},
            )
            assert init.status_code == 200, init.text
            up = init.json()
            upload_id = str(up["upload_id"])
            chunk_bytes = int(up.get("chunk_bytes") or 262144)

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
            assert done.json().get("ok") is True

            # 3) create job (enable QA)
            jobr = c.post(
                "/api/jobs",
                json={
                    "upload_id": upload_id,
                    "mode": "low",
                    "device": "cpu",
                    "src_lang": "auto",
                    "tgt_lang": "en",
                    "pg": "off",
                    "qa": True,
                    "cache_policy": "minimal",
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert jobr.status_code == 200, jobr.text
            job_id = str(jobr.json()["id"])

            # 4) poll until DONE/FAILED
            t0 = time.time()
            last_progress = -1.0
            state = ""
            job = {}
            while True:
                jr = c.get(f"/api/jobs/{job_id}")
                assert jr.status_code == 200, jr.text
                job = jr.json()
                state = str(job.get("state") or "")
                prog = float(job.get("progress") or 0.0)
                if prog > last_progress:
                    last_progress = prog
                if state in {"DONE", "FAILED", "CANCELED"}:
                    break
                if time.time() - t0 > 120:
                    raise AssertionError(f"job did not finish in time (state={state}, progress={prog})")
                time.sleep(0.5)

            if state != "DONE":
                # surface logs to make failures actionable
                try:
                    lg = c.get(f"/api/jobs/{job_id}/logs?n=200")
                    tail = lg.text[-2000:] if lg.status_code == 200 else lg.text
                except Exception:
                    tail = ""
                raise AssertionError(f"expected DONE, got {state} ({job.get('message')})\n{tail}")
            assert last_progress >= 0.1, "expected some progress updates"

            # 5) logs are accessible
            lg = c.get(f"/api/jobs/{job_id}/logs?n=200")
            assert lg.status_code == 200, lg.text

            # 6) outputs exist + mobile MP4 playable via Range
            fr = c.get(f"/api/jobs/{job_id}/files")
            assert fr.status_code == 200, fr.text
            files = fr.json()
            mobile = (files.get("mobile_mp4") or {}).get("url") or (files.get("mp4") or {}).get("url")
            assert mobile and str(mobile).startswith("/files/"), f"missing mobile mp4 url: {files.keys()}"

            rr = c.get(str(mobile), headers={"Range": "bytes=0-99"})
            assert rr.status_code == 206, rr.text
            assert rr.headers.get("accept-ranges") == "bytes"
            assert (rr.headers.get("content-range") or "").startswith("bytes ")
            assert int(rr.headers.get("content-length") or "0") == len(rr.content)

            # 7) QA artifacts exist and are fetchable
            qa_sum = (files.get("qa_summary") or {}).get("url")
            assert qa_sum and str(qa_sum).startswith("/files/"), "missing qa_summary artifact"
            q = c.get(str(qa_sum))
            assert q.status_code == 206, q.text  # /files always responds with 206
            # /files returns bytes; parse JSON from body
            qj = json.loads(q.content.decode("utf-8", errors="replace") or "{}")
            assert "score" in qj, f"qa summary missing score: {qj}"

            # 8) review endpoints work (force a minimal translated.json if needed)
            out_mkv = str(job.get("output_mkv") or "")
            assert out_mkv, "missing output_mkv"
            base_dir = Path(out_mkv).resolve().parent
            _write_minimal_translated_json(base_dir)
            # ensure review init/build works and returns a segment list
            rv = c.get(f"/api/jobs/{job_id}/review/segments")
            assert rv.status_code == 200, rv.text
            st = rv.json()
            segs = st.get("segments", [])
            assert isinstance(segs, list) and segs, "expected at least one review segment"

            # helper (rewrite)
            h = c.post(
                f"/api/jobs/{job_id}/review/segments/1/helper",
                json={"kind": "shorten10", "text": "This is a slightly long sentence."},
                headers={"X-CSRF-Token": csrf},
            )
            assert h.status_code == 200, h.text
            assert h.json().get("ok") is True

            # edit + regen + audio + lock/unlock
            e = c.post(
                f"/api/jobs/{job_id}/review/segments/1/edit",
                json={"text": "Hello."},
                headers={"X-CSRF-Token": csrf},
            )
            assert e.status_code == 200, e.text
            rg = c.post(
                f"/api/jobs/{job_id}/review/segments/1/regen",
                headers={"X-CSRF-Token": csrf},
            )
            assert rg.status_code == 200, rg.text
            au = c.get(
                f"/api/jobs/{job_id}/review/segments/1/audio",
                headers={"Range": "bytes=0-99"},
            )
            assert au.status_code in (200, 206), au.text
            lk = c.post(
                f"/api/jobs/{job_id}/review/segments/1/lock",
                headers={"X-CSRF-Token": csrf},
            )
            assert lk.status_code == 200, lk.text
            ul = c.post(
                f"/api/jobs/{job_id}/review/segments/1/unlock",
                headers={"X-CSRF-Token": csrf},
            )
            assert ul.status_code == 200, ul.text


def main() -> int:
    _verify_remote_modes()
    _verify_end_to_end()
    print("mobile_gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

