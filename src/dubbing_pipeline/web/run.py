from __future__ import annotations

import uvicorn

from dubbing_pipeline.config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "dubbing_pipeline.server:app",
        host=str(s.host),
        port=int(s.port),
        reload=False,
    )
