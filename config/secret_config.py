from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretConfig(BaseSettings):
    """
    Sensitive config.

    This module is safe to commit: it contains *no* secrets, only loading logic.
    Real secret values should come from:
      - environment variables (preferred in production)
      - optional local `.env.secrets` file (developer convenience)
    """

    model_config = SettingsConfigDict(
        env_file=".env.secrets",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- auth secrets (defaults preserve current dev behavior) ---
    jwt_secret: SecretStr = Field(default=SecretStr("dev-insecure-jwt-secret"), alias="JWT_SECRET")
    csrf_secret: SecretStr = Field(
        default=SecretStr("dev-insecure-csrf-secret"), alias="CSRF_SECRET"
    )
    session_secret: SecretStr = Field(
        default=SecretStr("dev-insecure-session-secret"), alias="SESSION_SECRET"
    )

    # Legacy/simple API key (used by the lightweight web UI auth)
    api_token: str = Field(default="change-me", alias="API_TOKEN")

    # optional admin bootstrap
    admin_username: str | None = Field(default=None, alias="ADMIN_USERNAME")
    admin_password: SecretStr | None = Field(default=None, alias="ADMIN_PASSWORD")

    # tokens/keys
    huggingface_token: SecretStr | None = Field(default=None, alias="HUGGINGFACE_TOKEN")
    hf_token: SecretStr | None = Field(default=None, alias="HF_TOKEN")  # legacy alias

    # storage / external URLs (treat as sensitive by default)
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    backup_s3_url: str | None = Field(default=None, alias="BACKUP_S3_URL")

    # character store encryption
    char_store_key: SecretStr | None = Field(default=None, alias="CHAR_STORE_KEY")

    # WebRTC TURN (optional)
    turn_url: str | None = Field(default=None, alias="TURN_URL")
    turn_username: str | None = Field(default=None, alias="TURN_USERNAME")
    turn_password: str | None = Field(default=None, alias="TURN_PASSWORD")

    # Notifications (optional; private/self-hosted ntfy)
    # Supported formats (examples):
    # - NTFY_AUTH=token:yourtoken
    # - NTFY_AUTH=Bearer yourtoken
    # - NTFY_AUTH=userpass:username:password
    # - NTFY_AUTH=username:password
    ntfy_auth: SecretStr | None = Field(default=None, alias="NTFY_AUTH")
