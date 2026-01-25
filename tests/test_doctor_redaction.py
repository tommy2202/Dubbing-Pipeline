from __future__ import annotations

from dubbing_pipeline.utils.doctor_redaction import redact


def test_redact_jwt_bearer() -> None:
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.signature"
    text = f"Authorization: Bearer {token}"
    out = redact(text)
    assert token not in out
    assert "Bearer ***REDACTED***" in out


def test_redact_kv_pairs() -> None:
    text = "API_TOKEN=abc123 JWT_SECRET: supersecret password = p@ss"
    out = redact(text)
    assert "API_TOKEN=***REDACTED***" in out
    assert "JWT_SECRET: ***REDACTED***" in out
    assert "password = ***REDACTED***" in out


def test_redact_known_tokens() -> None:
    text = (
        "dp_abcdef_ABCDEFGHIJKL ghp_012345678901234567890123456789012345 "
        "xoxb-123-456-789-abcdef sk-abcDEF123456789012345"
    )
    out = redact(text)
    assert "dp_abcdef_ABCDEFGHIJKL" not in out
    assert "ghp_012345678901234567890123456789012345" not in out
    assert "xoxb-123-456-789-abcdef" not in out
    assert "sk-abcDEF123456789012345" not in out
    assert out.count("***REDACTED***") >= 4


def test_redact_query_params() -> None:
    text = "https://example.com/?token=abc123&x=1"
    out = redact(text)
    assert "token=***REDACTED***" in out


def test_redact_no_change_for_plain_text() -> None:
    text = "hello world"
    out = redact(text)
    assert out == text
