from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    """
    Shutdown safety smoke test:
    - boot app via TestClient
    - login and hit library + dashboard endpoints
    - exit cleanly without shutdown exceptions
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        in_dir = root / "Input"
        out_dir = root / "Output"
        logs_dir = root / "logs"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        os.environ.setdefault("APP_ROOT", str(root))
        os.environ.setdefault("INPUT_DIR", str(in_dir))
        os.environ.setdefault("DUBBING_OUTPUT_DIR", str(out_dir))
        os.environ.setdefault("DUBBING_LOG_DIR", str(logs_dir))
        os.environ.setdefault("REMOTE_ACCESS_MODE", "off")
        os.environ.setdefault("COOKIE_SECURE", "0")
        os.environ.setdefault("ADMIN_USERNAME", "admin")
        os.environ.setdefault("ADMIN_PASSWORD", "password123")

        # Clear cached settings (important when running multiple verifies in same interpreter).
        try:
            from dubbing_pipeline.config import get_settings

            get_settings.cache_clear()
        except Exception:
            pass

        from dubbing_pipeline.server import app

        try:
            with TestClient(app) as c:
                r = c.get("/ui/login")
                assert r.status_code == 200, r.text

                r = c.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "password123", "session": True},
                )
                assert r.status_code == 200, r.text
                assert c.cookies.get("session"), "missing session cookie"
                assert c.cookies.get("csrf"), "missing csrf cookie"

                r = c.get("/ui/library")
                assert r.status_code == 200, r.text

                r = c.get("/ui/dashboard")
                assert r.status_code == 200, r.text
        except Exception as ex:
            print(f"verify_shutdown_clean: FAIL ({ex})", file=sys.stderr)
            raise

        log_path = logs_dir / "app.log"
        if log_path.exists():
            data = log_path.read_text(encoding="utf-8", errors="replace").lower()
            if "cancellederror" in data or "exceptiongroup" in data:
                print("verify_shutdown_clean: FAIL (CancelledError in logs)", file=sys.stderr)
                return 2

    print("verify_shutdown_clean: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
