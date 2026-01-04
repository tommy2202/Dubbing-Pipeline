from __future__ import annotations

import uvicorn

from anime_v2.config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "anime_v2.server:app",
        host=str(s.host),
        port=int(s.port),
        reload=False,
    )
