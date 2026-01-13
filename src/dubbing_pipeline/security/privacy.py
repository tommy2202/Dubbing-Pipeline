from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dubbing_pipeline.config import get_settings


@dataclass(frozen=True, slots=True)
class PrivacyOptions:
    privacy_on: bool
    no_store_transcript: bool
    no_store_source_audio: bool
    minimal_artifacts: bool

    def to_runtime_patch(self) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "privacy_mode": "on" if self.privacy_on else "off",
            "no_store_transcript": bool(self.no_store_transcript),
            "no_store_source_audio": bool(self.no_store_source_audio),
            "minimal_artifacts": bool(self.minimal_artifacts),
        }
        # Privacy implies data minimization; minimal retention is applied at job end.
        if self.privacy_on or self.minimal_artifacts:
            patch.setdefault("cache_policy", "minimal")
        return patch


def _truthy(v: object) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "on", "y"}


def resolve_privacy(runtime: dict[str, Any] | None = None) -> PrivacyOptions:
    """
    Resolve effective privacy knobs (runtime overrides take precedence).
    Defaults preserve current behavior (privacy OFF).
    """
    rt = dict(runtime or {}) if isinstance(runtime, dict) else {}
    s = get_settings()

    mode = (
        str(
            rt.get("privacy_mode")
            or rt.get("privacy")
            or getattr(s, "privacy_mode", "off")
            or "off"
        )
        .strip()
        .lower()
    )
    privacy_on = mode in {"on", "1", "true", "yes"}

    no_store_transcript = _truthy(rt.get("no_store_transcript")) or bool(
        getattr(s, "no_store_transcript", False)
    )
    no_store_source_audio = _truthy(rt.get("no_store_source_audio")) or bool(
        getattr(s, "no_store_source_audio", False)
    )
    minimal_artifacts = _truthy(rt.get("minimal_artifacts")) or bool(
        getattr(s, "minimal_artifacts", False)
    )

    # Privacy master switch turns on the strongest data minimization unless explicitly disabled
    # (there is currently no "--store-*" inverse, so this is conservative).
    if privacy_on:
        no_store_transcript = True
        no_store_source_audio = True
        minimal_artifacts = True

    return PrivacyOptions(
        privacy_on=bool(privacy_on),
        no_store_transcript=bool(no_store_transcript),
        no_store_source_audio=bool(no_store_source_audio),
        minimal_artifacts=bool(minimal_artifacts),
    )
