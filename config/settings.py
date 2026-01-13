from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pydantic import SecretStr

from .public_config import PublicConfig
from .secret_config import SecretConfig


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    """
    Merged settings view (dot-access).

    Precedence:
      - secrets override public when names overlap
    """

    public: PublicConfig
    secret: SecretConfig

    def __getattr__(self, name: str) -> Any:
        if hasattr(self.secret, name):
            return getattr(self.secret, name)
        return getattr(self.public, name)

    # Backwards-compat: preserve existing helper API from `dubbing_pipeline.config.Settings`
    def cors_origin_list(self) -> list[str]:
        return self.public.cors_origin_list()


def _is_insecure_default(secret: SecretStr, marker: str) -> bool:
    try:
        return secret.get_secret_value() == marker
    except Exception:
        return False


def _validate_secrets(s: Settings) -> None:
    """
    Hard-fail only when explicitly requested.

    This preserves current behavior (dev-insecure defaults) unless the user opts in.
    """
    import logging

    # Prefer env var to avoid having to add another public setting.
    strict = bool(int(__import__("os").environ.get("STRICT_SECRETS", "0") or "0"))

    weak: list[str] = []
    if _is_insecure_default(s.secret.jwt_secret, "dev-insecure-jwt-secret"):
        weak.append("JWT_SECRET")
    if _is_insecure_default(s.secret.csrf_secret, "dev-insecure-csrf-secret"):
        weak.append("CSRF_SECRET")
    if _is_insecure_default(s.secret.session_secret, "dev-insecure-session-secret"):
        weak.append("SESSION_SECRET")
    if str(s.secret.api_token or "") == "change-me":
        weak.append("API_TOKEN")

    # Optional admin bootstrap: reject common placeholders in strict mode; warn otherwise.
    try:
        apw = s.secret.admin_password.get_secret_value() if s.secret.admin_password else ""
    except Exception:
        apw = ""
    if apw and apw.strip().lower() in {"change-me", "admin", "adminpass", "password", "123456"}:
        weak.append("ADMIN_PASSWORD")

    if weak:
        if strict:
            raise ConfigError(
                "Weak/insecure secrets detected: "
                + ", ".join(sorted(set(weak)))
                + ". Set them via environment variables or `.env.secrets`."
            )
        logging.getLogger("dubbing_pipeline").warning(
            "weak_secrets_detected",
            extra={"weak": sorted(set(weak)), "strict_secrets": False},
        )

    # Remote-mode hardening warnings (never fail boot automatically; fail is via STRICT_SECRETS).
    try:
        mode = str(getattr(s.public, "remote_access_mode", "off") or "off").strip().lower()
    except Exception:
        mode = "off"
    if mode != "off":
        warnings: list[str] = []
        if not bool(getattr(s.public, "cookie_secure", False)):
            warnings.append("COOKIE_SECURE=0 (cookies not marked Secure)")
        if not str(getattr(s.public, "cors_origins", "") or "").strip():
            warnings.append("CORS_ORIGINS empty (browser clients may be less constrained)")
        if bool(getattr(s.public, "allow_legacy_token_login", False)):
            warnings.append("ALLOW_LEGACY_TOKEN_LOGIN=1 (unsafe on public networks)")
        if mode == "cloudflare" and not bool(getattr(s.public, "trust_proxy_headers", False)):
            warnings.append("TRUST_PROXY_HEADERS=0 (may break HTTPS detection behind Cloudflare)")
        if warnings:
            logging.getLogger("dubbing_pipeline").warning(
                "remote_mode_hardening_warning",
                extra={"mode": mode, "warnings": warnings},
            )


def get_safe_config_report() -> dict[str, Any]:
    """
    Deterministic, non-sensitive config report.

    - Public values are included (paths are stringified)
    - Secret values are NEVER included; only SET/UNSET markers
    """
    s = get_settings()

    pub = s.public.model_dump()
    pub_s: dict[str, Any] = {}
    for k, v in pub.items():
        # stringify Paths for stable JSON output
        try:
            pub_s[k] = str(v) if hasattr(v, "__fspath__") else v
        except Exception:
            pub_s[k] = str(v)

    sec_fields = sorted(list(s.secret.model_fields.keys()))
    sec: dict[str, str] = {}
    for k in sec_fields:
        try:
            v = getattr(s.secret, k)
        except Exception:
            sec[k] = "UNSET"
            continue
        if v is None:
            sec[k] = "UNSET"
        elif isinstance(v, SecretStr):
            sec[k] = "SET" if v.get_secret_value() else "UNSET"
        else:
            sec[k] = "SET" if str(v).strip() else "UNSET"

    strict = bool(int(__import__("os").environ.get("STRICT_SECRETS", "0") or "0"))
    return {
        "strict_secrets": strict,
        "public": pub_s,
        "secrets": sec,
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    public = PublicConfig()
    secret = SecretConfig()
    s = Settings(public=public, secret=secret)
    _validate_secrets(s)
    return s


class _SettingsProxy:
    """
    Lazy proxy so tests can set env vars before first access.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def reload(self) -> None:
        get_settings.cache_clear()

    def snapshot(self) -> Settings:
        return get_settings()


# Single access point
SETTINGS = _SettingsProxy()
