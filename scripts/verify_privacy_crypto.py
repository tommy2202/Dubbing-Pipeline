from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path


def _rand_key_b64() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def main() -> int:
    # 1) Crypto roundtrip (bytes + file)
    key_b64 = _rand_key_b64()
    os.environ["ENCRYPT_AT_REST"] = "1"
    os.environ["ENCRYPT_AT_REST_CLASSES"] = "review"
    os.environ["ARTIFACTS_KEY"] = key_b64

    from dubbing_pipeline.config import get_settings

    get_settings.cache_clear()

    from dubbing_pipeline.security.crypto import (
        CryptoConfigError,
        decrypt_bytes,
        decrypt_file,
        encrypt_bytes,
        encrypt_file,
    )

    pt = b"hello world" * 1000
    ct = encrypt_bytes(pt, kind="review", job_id="j_test")
    pt2 = decrypt_bytes(ct, kind="review", job_id="j_test")
    assert pt2 == pt

    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.bin")
        enc = os.path.join(td, "in.bin.enc")
        dec = os.path.join(td, "out.bin")
        with open(inp, "wb") as f:
            f.write(os.urandom(8 * 1024 * 1024))
        encrypt_file(inp, enc, kind="review", job_id="j_test")  # type: ignore[arg-type]
        decrypt_file(enc, dec, kind="review", job_id="j_test")  # type: ignore[arg-type]
        assert Path(inp).read_bytes() == Path(dec).read_bytes()

    # 2) Fail-safe: encryption enabled but key missing must error (no silent plaintext writes)
    os.environ["ARTIFACTS_KEY"] = ""
    get_settings.cache_clear()
    try:
        _ = encrypt_bytes(b"x", kind="review", job_id="j_test")
        raise AssertionError("expected CryptoConfigError when ARTIFACTS_KEY missing")
    except CryptoConfigError:
        pass

    # 3) Privacy mode resolution
    from dubbing_pipeline.security.privacy import resolve_privacy

    p0 = resolve_privacy({})
    assert p0.privacy_on is False

    p1 = resolve_privacy({"privacy_mode": "on"})
    assert p1.privacy_on is True
    assert p1.no_store_transcript is True
    assert p1.no_store_source_audio is True
    assert p1.minimal_artifacts is True
    patch = p1.to_runtime_patch()
    assert patch.get("cache_policy") == "minimal"

    print("verify_privacy_crypto: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

