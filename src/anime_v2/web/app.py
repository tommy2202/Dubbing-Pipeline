from __future__ import annotations

from fastapi import FastAPI

from anime_v2.utils.log import logger

app = FastAPI(title="anime_v2 web")


@app.get("/health")
def health() -> dict[str, str]:
    logger.info("[v2] health check")
    return {"status": "ok"}

