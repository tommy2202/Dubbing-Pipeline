from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DUBBING_LOG_DIR"] = str(log_dir)
        os.environ["LOG_TRANSCRIPTS"] = "0"

        from dubbing_pipeline.config import get_settings
        from dubbing_pipeline.ops import audit
        from dubbing_pipeline.utils.log import _redact_str, safe_log_data

        get_settings.cache_clear()

        data = safe_log_data({"transcript": "secret transcript", "text": "hello"})
        assert isinstance(data, dict)
        assert data["transcript"]["redacted"] is True
        assert data["text"]["redacted"] is True

        raw = "Authorization: Bearer eyJabc.def.ghi cookie=session=abc"
        red = _redact_str(raw)
        assert "***REDACTED***" in red
        assert "eyJ" not in red
        assert "session=abc" not in red

        audit.event(
            "privacy.verify",
            actor_id="u_1",
            resource_id="job_1",
            request_id="req_1",
            outcome="ok",
            meta_safe={"transcript": "secret", "path": "/tmp/secret.txt"},
        )

        audit_path = log_dir / "audit.jsonl"
        assert audit_path.exists()
        rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
        assert rec.get("event_type") == "privacy.verify"
        assert rec.get("actor_id") == "u_1"
        assert rec.get("resource_id") == "job_1"
        assert rec.get("outcome") == "ok"
        meta = rec.get("meta") or {}
        assert meta.get("transcript", {}).get("redacted") is True
        assert meta.get("path", {}).get("redacted") is True

        print("verify_privacy_logging: ok")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
