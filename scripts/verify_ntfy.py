from __future__ import annotations

import sys

from dubbing_pipeline.config import get_settings


def main() -> int:
    s = get_settings()

    enabled = bool(getattr(s, "ntfy_enabled", False))
    base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    topic = str(getattr(s, "ntfy_topic", "") or "").strip()

    if not enabled:
        print("verify_ntfy: NTFY_ENABLED=0 (skipping)")
        return 0

    if not base or not topic:
        print("verify_ntfy: enabled but NTFY_BASE_URL/NTFY_TOPIC not configured (skipping)", file=sys.stderr)
        return 0

    from dubbing_pipeline.notify.ntfy import notify

    ok = notify(
        event="verify.ntfy",
        title="dubbing_pipeline ntfy test",
        message="This is a test notification from scripts/verify_ntfy.py",
        url=(str(getattr(s, "public_base_url", "") or "").strip().rstrip("/") + "/ui/dashboard")
        if str(getattr(s, "public_base_url", "") or "").strip()
        else None,
        tags=["dubbing-pipeline", "test"],
        priority=3,
        user_id=None,
        job_id=None,
    )
    if not ok:
        print("verify_ntfy: failed to deliver notification (check ntfy config/auth)", file=sys.stderr)
        return 1

    print("verify_ntfy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

