from __future__ import annotations

import os

from starlette.requests import Request

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.net import get_client_ip


def _make_request(peer_ip: str, headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": (peer_ip, 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }
    return Request(scope)


def test_untrusted_proxy_ignores_xff() -> None:
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    os.environ["TRUSTED_PROXIES"] = "198.51.100.1"
    get_settings.cache_clear()

    req = _make_request(
        "203.0.113.10",
        {"x-forwarded-for": "10.0.0.5, 198.51.100.99"},
    )
    assert get_client_ip(req) == "203.0.113.10"


def test_trusted_proxy_honors_xff_public() -> None:
    os.environ["TRUST_PROXY_HEADERS"] = "1"
    os.environ["TRUSTED_PROXIES"] = "203.0.113.10"
    get_settings.cache_clear()

    req = _make_request(
        "203.0.113.10",
        {"x-forwarded-for": "10.0.0.5, 198.51.100.99"},
    )
    assert get_client_ip(req) == "198.51.100.99"
