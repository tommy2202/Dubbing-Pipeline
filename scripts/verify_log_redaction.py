from __future__ import annotations

import json
import os

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.log import safe_log_data


def main() -> int:
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaa.bbbb"
    api_key = "dp_abcdef_ABCDEFGHIJKLMNOP"
    cookie = "session=sessionvalue; refresh=refreshvalue; csrf=csrfvalue"
    headers = {"Authorization": f"Bearer {jwt}", "X-Api-Key": api_key, "Cookie": cookie}

    os.environ["JWT_SECRET"] = "super-secret-value-12345"
    get_settings.cache_clear()

    payload = {
        "headers": headers,
        "authorization": f"Bearer {jwt}",
        "cookie": cookie,
        "token": jwt,
        "msg": "jwt=super-secret-value-12345",
    }
    redacted = safe_log_data(payload)
    print("sample_log:", json.dumps(redacted, sort_keys=True))

    s = json.dumps(redacted)
    if any(x in s for x in [jwt, api_key, "sessionvalue", "refreshvalue", "csrfvalue"]):
        print("FAIL: token or cookie leaked")
        return 1
    if "super-secret-value-12345" in s:
        print("FAIL: secret literal leaked")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
