"""
Legacy entrypoint shim.

Historically, this repo had a second, simplified FastAPI app in the repo root (`main.py`)
with its own upload + `/dub` endpoint that called the legacy pipeline.

To avoid conflicts and ensure **one canonical web implementation**, this file now
re-exports the hardened server app from `src/dubbing_pipeline/server.py`.

Keeping this shim preserves compatibility with:
- `uvicorn main:app` (repo-root patterns)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `src/` is importable when running `uvicorn main:app` from repo root.
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dubbing_pipeline.server import app  # noqa: E402,F401


def main() -> None:  # pragma: no cover
    import uvicorn

    from dubbing_pipeline.config import get_settings

    s = get_settings()
    uvicorn.run(
        "main:app",
        host=str(s.host),
        port=int(s.port),
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
