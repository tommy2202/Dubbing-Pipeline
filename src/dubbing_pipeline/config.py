"""
Backwards-compatible settings shim.

The repo's canonical config now lives in `config/`:
  - `config/public_config.py` (non-sensitive defaults)
  - `config/secret_config.py` (secrets loaded from env / `.env.secrets`)
  - `config/settings.py` exposes `SETTINGS`

Existing imports (`from dubbing_pipeline.config import get_settings`) continue to work.
"""

from __future__ import annotations

from config.settings import Settings as Settings
from config.settings import get_settings as get_settings
