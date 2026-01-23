#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from dubbing_pipeline.config import get_settings


def _payload(url: str | None) -> dict:
    return {
        "event": "verify.ntfy.notifications",
        "title": "dubbing_pipeline notification test",
        "message": "Test notification for private ntfy delivery.",
        "url": url,
        "tags": ["dubbing-pipeline", "test"],
        "priority": 3,
    }


def main() -> int:
    s = get_settings()
    enabled = bool(getattr(s, "ntfy_enabled", False))
    base = str(getattr(s, "ntfy_base_url", "") or "").strip()
    topic = str(getattr(s, "ntfy_topic", "") or "").strip()

    base_url = str(getattr(s, "public_base_url", "") or "").strip().rstrip("/")
    click = f"{base_url}/ui/dashboard" if base_url else None
    payload = _payload(click)

    if not enabled or not base or not topic:
        print("verify_ntfy_notifications: DRY_RUN (ntfy not configured)")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    from dubbing_pipeline.notify.ntfy import notify

    ok = notify(
        event=payload["event"],
        title=payload["title"],
        message=payload["message"],
        url=payload["url"],
        tags=payload["tags"],
        priority=payload["priority"],
        user_id=None,
        job_id=None,
        topic=topic,
    )
    if not ok:
        print("verify_ntfy_notifications: DRY_RUN (delivery failed)", file=sys.stderr)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print("verify_ntfy_notifications: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
