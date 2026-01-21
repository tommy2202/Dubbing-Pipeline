from __future__ import annotations

import json
import os

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import safe_log_data


def test_log_redaction_tokens() -> None:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaa.bbbb"
    api_key = "dp_abcdef_ABCDEFGHIJKLMNOP"
    cookie = "session=sessionvalue; refresh=refreshvalue; csrf=csrfvalue"
    headers = {"Authorization": f"Bearer {jwt}", "X-Api-Key": api_key, "Cookie": cookie}
    payload = {
        "headers": headers,
        "authorization": f"Bearer {jwt}",
        "cookie": cookie,
        "token": jwt,
        "transcript": "hello world",
        "segments": [{"text": "hi"}],
    }
    redacted = safe_log_data(payload)
    s = json.dumps(redacted)
    assert jwt not in s
    assert api_key not in s
    assert "sessionvalue" not in s
    assert "refreshvalue" not in s
    assert "csrfvalue" not in s
    assert isinstance(redacted.get("transcript"), dict)
    assert redacted.get("transcript", {}).get("redacted") is True
    assert isinstance(redacted.get("segments"), dict)
    assert redacted.get("segments", {}).get("count") == 1


def test_log_redaction_secret_literals() -> None:
    os.environ["JWT_SECRET"] = "super-secret-value-12345"
    get_settings.cache_clear()
    redacted = safe_log_data({"msg": "jwt=super-secret-value-12345"})
    s = json.dumps(redacted)
    assert "super-secret-value-12345" not in s
