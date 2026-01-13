from __future__ import annotations

import os
from concurrent.futures import CancelledError as FuturesCancelledError
from pathlib import Path

from fastapi.testclient import TestClient


def _login(c: TestClient, username: str, password: str) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "session": True})
    assert r.status_code in (200, 401), r.text
    if r.status_code == 401:
        return {}
    d = r.json()
    return {"X-CSRF-Token": d["csrf_token"]}


def _scan_dir_for_needles(root: Path, needles: list[str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if not root.exists():
        return hits
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip big media artifacts
        if p.suffix.lower() in {".mp4", ".mkv", ".wav", ".npy", ".ts"}:
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for n in needles:
            if n and n in data:
                hits.append((str(p), n))
    return hits


def main() -> int:
    # Use unique secrets so we can detect any leakage.
    jwt = "jwt_secret_TEST_DO_NOT_LOG_9f6c1f1e"
    sess = "session_secret_TEST_DO_NOT_LOG_1f5c0b88"
    csrf = "csrf_secret_TEST_DO_NOT_LOG_4a1f8b0c"
    api = "api_token_TEST_DO_NOT_LOG_3b7c2d1a"
    admin_pw = "admin_password_TEST_DO_NOT_LOG_8c2e7f90"

    log_dir = Path("/tmp/dubbing_pipeline_no_leak_logs").resolve()
    out_dir = Path("/tmp/dubbing_pipeline_no_leak_out").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ["APP_ROOT"] = "/workspace"
    os.environ["DUBBING_LOG_DIR"] = str(log_dir)
    os.environ["DUBBING_OUTPUT_DIR"] = str(out_dir)
    os.environ["COOKIE_SECURE"] = "0"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = admin_pw
    os.environ["JWT_SECRET"] = jwt
    os.environ["SESSION_SECRET"] = sess
    os.environ["CSRF_SECRET"] = csrf
    os.environ["API_TOKEN"] = api
    os.environ["STRICT_SECRETS"] = "1"

    from dubbing_pipeline.config import get_settings
    from dubbing_pipeline.server import app

    get_settings.cache_clear()

    try:
        with TestClient(app) as c:
            # failed login (should audit + not leak password)
            _ = c.post("/api/auth/login", json={"username": "admin", "password": "wrong", "session": True})

            hdr = _login(c, "admin", admin_pw)
            assert hdr

            # create an API key (response contains key; must never show up in logs due to masking)
            r = c.post("/keys", json={"scopes": ["read:job"]}, headers=hdr)
            assert r.status_code == 200, r.text
            key_plain = r.json()["key"]

            # Use the key in a request (should not leak in logs)
            r2 = c.get("/api/jobs?limit=1", headers={"X-Api-Key": key_plain})
            assert r2.status_code in (200, 403), r2.text

            # Create a trivial upload init + ensure audit emits
            r3 = c.post(
                "/api/uploads/init",
                json={"filename": "x.mp4", "total_bytes": 1024},
                headers=hdr,
            )
            assert r3.status_code == 200, r3.text
    except FuturesCancelledError:
        # Some Starlette/AnyIO combinations can raise CancelledError on shutdown.
        # The assertions above already ran; treat shutdown cancellation as clean exit.
        pass

    needles = [jwt, sess, csrf, api, admin_pw, key_plain]
    hits = []
    hits.extend(_scan_dir_for_needles(log_dir, needles))
    hits.extend(_scan_dir_for_needles(out_dir / "jobs", needles))

    if hits:
        for fp, n in hits[:20]:
            print(f"LEAK: found needle in {fp}: {n}")
        raise SystemExit(2)

    print("verify_no_secret_leaks: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

