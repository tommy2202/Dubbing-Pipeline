from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dubbing_pipeline.config import get_settings


def main() -> int:
    s = get_settings()
    origins = s.cors_origin_list()
    samesite = str(getattr(s, "cookie_samesite", "lax") or "lax").strip().lower()
    secure = bool(getattr(s, "cookie_secure", False))
    env = str(os.environ.get("ENV") or os.environ.get("APP_ENV") or "").strip().lower()

    print("CORS/CSRF effective policy (safe)")
    print(f"- ENV: {env or 'development'}")
    print(f"- CORS_ORIGINS: {origins if origins else '[]'}")
    print(f"- COOKIE_SECURE: {secure}")
    print(f"- COOKIE_SAMESITE: {samesite}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
