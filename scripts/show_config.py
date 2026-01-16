from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    # scripts/ is at repo root; resolve from this file location
    return Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict[str, str]:
    """
    Minimal .env parser:
    - supports KEY=VALUE
    - ignores blank lines and comments
    - strips optional surrounding quotes
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # drop optional quotes (best-effort)
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def _which(cmd: str) -> bool:
    import shutil

    return shutil.which(cmd) is not None


@dataclass(frozen=True)
class Item:
    key: str
    value: str
    source: str


def _redact(key: str, value: Any) -> str:
    k = key.upper()
    if any(x in k for x in ("SECRET", "PASSWORD", "TOKEN", "KEY", "AUTH")):
        return "(set)" if str(value or "").strip() else "(unset)"
    s = str(value)
    if len(s) > 180:
        s = s[:177] + "..."
    return s


def _source_for_key(key: str, env: dict[str, str], env_file: dict[str, str], secrets_file: dict[str, str]):
    if key in env:
        return "env"
    if key in secrets_file:
        return ".env.secrets"
    if key in env_file:
        return ".env"
    return "default"


def _print_kv(title: str, items: list[Item]) -> None:
    print("")
    print(title)
    print("-" * len(title))
    width = max((len(i.key) for i in items), default=10)
    for it in items:
        print(f"{it.key:<{width}}  {it.value}  [{it.source}]")


def main() -> int:
    root = _repo_root()
    env_path = root / ".env"
    secrets_path = root / ".env.secrets"

    env_file = _parse_env_file(env_path)
    secrets_file = _parse_env_file(secrets_path)
    env = dict(os.environ)

    # Import after parsing so we can show both "raw source" and "effective values".
    try:
        from dubbing_pipeline.config import get_settings
    except Exception as ex:
        print("ERROR: Python package not installed / import failed.")
        print("Run from repo root:")
        print("  python3 -m pip install -e .")
        print(f"Import error: {ex}")
        return 2

    s = get_settings()

    print("Dubbing Pipeline — effective configuration (safe report)")
    print("")
    print("Legend: [env] overrides [.env.secrets] overrides [.env] overrides [default]")
    print("")

    # Core runtime endpoints
    host = str(getattr(s, "host", "0.0.0.0"))
    port = int(getattr(s, "port", 8000))
    mode = str(getattr(s, "remote_access_mode", "off") or "off")
    output_dir = str(getattr(s, "output_dir", "Output"))
    log_dir = str(getattr(s, "log_dir", "logs"))
    state_dir = str(getattr(s, "state_dir", "") or (Path(output_dir) / "_state"))

    # Redis is optional; when enabled it powers Level-2 queue state/locks/counters.
    redis_url = str(getattr(s, "redis_url", "") or "")
    redis_enabled = bool(redis_url.strip())
    queue_mode_cfg = str(getattr(s, "queue_mode", "auto") or "auto").strip().lower()

    items = [
        Item("HOST", _redact("HOST", host), _source_for_key("HOST", env, env_file, secrets_file)),
        Item("PORT", _redact("PORT", port), _source_for_key("PORT", env, env_file, secrets_file)),
        Item(
            "REMOTE_ACCESS_MODE",
            _redact("REMOTE_ACCESS_MODE", mode),
            _source_for_key("REMOTE_ACCESS_MODE", env, env_file, secrets_file),
        ),
        Item(
            "OUTPUT_DIR",
            _redact("DUBBING_OUTPUT_DIR", output_dir),
            _source_for_key("DUBBING_OUTPUT_DIR", env, env_file, secrets_file),
        ),
        Item(
            "LOG_DIR",
            _redact("DUBBING_LOG_DIR", log_dir),
            _source_for_key("DUBBING_LOG_DIR", env, env_file, secrets_file),
        ),
        Item(
            "STATE_DIR",
            _redact("DUBBING_STATE_DIR", state_dir),
            _source_for_key("DUBBING_STATE_DIR", env, env_file, secrets_file),
        ),
        Item("REDIS_URL", _redact("REDIS_URL", redis_url), _source_for_key("REDIS_URL", env, env_file, secrets_file)),
        Item("REDIS_ENABLED", str(redis_enabled), "computed"),
        Item(
            "QUEUE_MODE",
            _redact("QUEUE_MODE", queue_mode_cfg),
            _source_for_key("QUEUE_MODE", env, env_file, secrets_file),
        ),
    ]
    _print_kv("Core", items)

    # Security toggles (non-secret)
    sec_items = [
        Item(
            "COOKIE_SECURE",
            str(bool(getattr(s, "cookie_secure", False))),
            _source_for_key("COOKIE_SECURE", env, env_file, secrets_file),
        ),
        Item(
            "CORS_ORIGINS",
            _redact("CORS_ORIGINS", str(getattr(s, "cors_origins", "") or "")),
            _source_for_key("CORS_ORIGINS", env, env_file, secrets_file),
        ),
        Item(
            "ALLOW_LEGACY_TOKEN_LOGIN",
            str(bool(getattr(s, "allow_legacy_token_login", False))),
            _source_for_key("ALLOW_LEGACY_TOKEN_LOGIN", env, env_file, secrets_file),
        ),
        Item(
            "STRICT_SECRETS",
            str(bool(int(os.environ.get("STRICT_SECRETS", "0") or "0"))),
            _source_for_key("STRICT_SECRETS", env, env_file, secrets_file),
        ),
        Item(
            "JWT_SECRET",
            _redact("JWT_SECRET", secrets_file.get("JWT_SECRET", "")),
            _source_for_key("JWT_SECRET", env, env_file, secrets_file),
        ),
        Item(
            "SESSION_SECRET",
            _redact("SESSION_SECRET", secrets_file.get("SESSION_SECRET", "")),
            _source_for_key("SESSION_SECRET", env, env_file, secrets_file),
        ),
        Item(
            "CSRF_SECRET",
            _redact("CSRF_SECRET", secrets_file.get("CSRF_SECRET", "")),
            _source_for_key("CSRF_SECRET", env, env_file, secrets_file),
        ),
        Item(
            "ADMIN_USERNAME",
            _redact("ADMIN_USERNAME", secrets_file.get("ADMIN_USERNAME", "")),
            _source_for_key("ADMIN_USERNAME", env, env_file, secrets_file),
        ),
        Item(
            "ADMIN_PASSWORD",
            _redact("ADMIN_PASSWORD", secrets_file.get("ADMIN_PASSWORD", "")),
            _source_for_key("ADMIN_PASSWORD", env, env_file, secrets_file),
        ),
    ]
    _print_kv("Security (safe)", sec_items)

    # Limits & scheduler behavior (what a non-programmer needs to understand)
    lim_items = [
        Item(
            "JOBS_CONCURRENCY",
            str(int(getattr(s, "jobs_concurrency", 1))),
            _source_for_key("JOBS_CONCURRENCY", env, env_file, secrets_file),
        ),
        Item(
            "MAX_CONCURRENCY_GLOBAL",
            str(int(getattr(s, "max_concurrency_global", 2))),
            _source_for_key("MAX_CONCURRENCY_GLOBAL", env, env_file, secrets_file),
        ),
        Item(
            "MAX_CONCURRENCY_TRANSCRIBE",
            str(int(getattr(s, "max_concurrency_transcribe", 1))),
            _source_for_key("MAX_CONCURRENCY_TRANSCRIBE", env, env_file, secrets_file),
        ),
        Item(
            "MAX_CONCURRENCY_TTS",
            str(int(getattr(s, "max_concurrency_tts", 1))),
            _source_for_key("MAX_CONCURRENCY_TTS", env, env_file, secrets_file),
        ),
        Item(
            "BACKPRESSURE_Q_MAX",
            str(int(getattr(s, "backpressure_q_max", 6))),
            _source_for_key("BACKPRESSURE_Q_MAX", env, env_file, secrets_file),
        ),
        Item(
            "MAX_JOBS_HIGH",
            str(int(getattr(s, "max_jobs_high", 1))),
            _source_for_key("MAX_JOBS_HIGH", env, env_file, secrets_file),
        ),
        Item(
            "MAX_JOBS_MEDIUM",
            str(int(getattr(s, "max_jobs_medium", 0))),
            _source_for_key("MAX_JOBS_MEDIUM", env, env_file, secrets_file),
        ),
        Item(
            "MAX_JOBS_LOW",
            str(int(getattr(s, "max_jobs_low", 0))),
            _source_for_key("MAX_JOBS_LOW", env, env_file, secrets_file),
        ),
        Item(
            "HIGH_MODE_ADMIN_ONLY",
            str(bool(getattr(s, "high_mode_admin_only", True))),
            _source_for_key("DUBBING_HIGH_MODE_ADMIN_ONLY", env, env_file, secrets_file),
        ),
        Item(
            "MAX_ACTIVE_JOBS_PER_USER",
            str(int(getattr(s, "max_active_jobs_per_user", 1))),
            _source_for_key("DUBBING_MAX_ACTIVE_JOBS_PER_USER", env, env_file, secrets_file),
        ),
        Item(
            "MAX_QUEUED_JOBS_PER_USER",
            str(int(getattr(s, "max_queued_jobs_per_user", 5))),
            _source_for_key("DUBBING_MAX_QUEUED_JOBS_PER_USER", env, env_file, secrets_file),
        ),
    ]
    _print_kv("Limits", lim_items)

    print("")
    print("Behavior notes")
    print("--------------")
    print("- Queue/execution: jobs run on this server process (in-proc workers).")
    print("- Backpressure: if the internal scheduler queue is too long, mode may degrade: high -> medium -> low.")
    print("- Redis (if configured): provides Level-2 queue state + locks + counters; falls back to local queue if unavailable.")
    print("")

    # Useful operator hints
    print("Paths")
    print("-----")
    print(f"- Repo root: {root}")
    print(f"- Output:    {output_dir}")
    print(f"- Logs:      {log_dir}")
    print(f"- State:     {state_dir}  (jobs.db + auth.db live here)")
    print("")
    print("Tooling")
    print("-------")
    print(f"- python: {'OK' if sys.version_info >= (3, 10) else 'OLD'} ({sys.version.split()[0]})")
    print(f"- ffmpeg: {'OK' if _which('ffmpeg') else 'MISSING'}")
    print(f"- ffprobe: {'OK' if _which('ffprobe') else 'MISSING'}")
    print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

