from __future__ import annotations

import os

from config.settings import get_safe_config_report, get_settings
from dubbing_pipeline.utils.log import _redact_str


def main() -> int:
    os.environ["JWT_SECRET"] = "supersecretjwtvalue1234567890"
    os.environ["SESSION_SECRET"] = "sessionsecretvalue1234567890"
    os.environ["CSRF_SECRET"] = "csrfsecretvalue1234567890"
    os.environ["API_TOKEN"] = "change-me"
    os.environ["ENV"] = "development"

    get_settings.cache_clear()

    raw = "jwt_secret=supersecretjwtvalue1234567890 session_secret=sessionsecretvalue1234567890"
    redacted = _redact_str(raw)
    if "supersecretjwtvalue1234567890" in redacted or "sessionsecretvalue1234567890" in redacted:
        raise SystemExit("redaction failed")

    report = get_safe_config_report()
    secrets = report.get("secrets", {})
    if isinstance(secrets, dict):
        for v in secrets.values():
            if isinstance(v, str) and "secret" in v.lower():
                raise SystemExit("safe config report leaked secret")

    print("verify_secrets_redaction: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
