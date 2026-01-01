#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.public_config import PublicConfig  # noqa: E402
from config.secret_config import SecretConfig  # noqa: E402
from config.settings import get_safe_config_report, get_settings  # noqa: E402

PAT_ENV = re.compile(r"(os\.environ\[|os\.getenv\(|environ\.get\()")


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _assert_no_env_reads_outside_config_and_tests() -> None:
    allow_files = {
        REPO_ROOT / "config" / "public_config.py",
        REPO_ROOT / "config" / "settings.py",
        # Verification / smoke scripts may set process-level env defaults.
        REPO_ROOT / "scripts" / "smoke_run.py",
        REPO_ROOT / "scripts" / "verify_features.py",
    }

    offenders: list[Path] = []
    for p in REPO_ROOT.rglob("*.py"):
        if "tests" in p.parts:
            continue
        if p in allow_files:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if PAT_ENV.search(txt):
            offenders.append(p)

    if offenders:
        lines = "\n".join(str(p.relative_to(REPO_ROOT)) for p in sorted(offenders))
        _fail(f"Found direct env reads outside config/tests:\n{lines}")


def _assert_config_keys_exist() -> None:
    pub_keys = set(PublicConfig.model_fields.keys())
    sec_keys = set(SecretConfig.model_fields.keys())
    if not pub_keys:
        _fail("PublicConfig has no fields")
    if not sec_keys:
        _fail("SecretConfig has no fields")

    s = get_settings()
    for k in pub_keys:
        getattr(s.public, k)
    for k in sec_keys:
        getattr(s.secret, k)


def _assert_required_public_values() -> None:
    s = get_settings()
    if not str(s.host).strip():
        _fail("HOST is empty")
    if int(s.port) <= 0:
        _fail("PORT must be > 0")
    for name in ("app_root", "output_dir", "log_dir"):
        p = Path(getattr(s, name))
        if not str(p).strip():
            _fail(f"{name} is empty")

    # Ensure these are usable paths (create best-effort).
    for p in [Path(s.output_dir), Path(s.log_dir)]:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as ex:
            _fail(f"Cannot create configured directory {p}: {ex}")


def _assert_required_secrets_when_needed() -> None:
    """
    "Required when needed":
    - If STRICT_SECRETS=1: require core secrets and API_TOKEN
    - If ENABLE_PYANNOTE and diarizer implies pyannote: require HF token
    - If TURN_URL is set: require TURN_USERNAME + TURN_PASSWORD
    """
    s = get_settings()

    strict = bool(get_safe_config_report().get("strict_secrets"))
    if strict:
        rep = get_safe_config_report()["secrets"]
        for k in ("jwt_secret", "csrf_secret", "session_secret", "api_token"):
            if rep.get(k) != "SET":
                _fail(f"STRICT_SECRETS=1 but {k} is not set")

    diarizer = str(s.diarizer).lower()
    if bool(s.enable_pyannote) and diarizer in {"auto", "pyannote"}:
        tok = s.huggingface_token or s.hf_token
        if tok is None or not tok.get_secret_value().strip():
            _fail("ENABLE_PYANNOTE=1 but HUGGINGFACE_TOKEN/HF_TOKEN is not set")

    if s.turn_url and not (s.turn_username and s.turn_password):
        _fail("TURN_URL is set but TURN_USERNAME/TURN_PASSWORD is missing")


def main() -> int:
    _assert_no_env_reads_outside_config_and_tests()
    _assert_config_keys_exist()
    _assert_required_public_values()
    _assert_required_secrets_when_needed()

    report = get_safe_config_report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
