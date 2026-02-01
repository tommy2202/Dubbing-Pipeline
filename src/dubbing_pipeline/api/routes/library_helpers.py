from __future__ import annotations

from fastapi import HTTPException, Request


_REPORT_REASON_MAX = 200


def _store(request: Request):
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise RuntimeError("Job store not initialized")
    return store


def _parse_library_key(key: str) -> tuple[str, int, int]:
    raw = str(key or "").strip()
    parts = raw.split(":")
    if len(parts) != 3:
        raise HTTPException(
            status_code=400, detail="Invalid library key (expected series_slug:season:episode)"
        )
    slug = str(parts[0] or "").strip()
    try:
        season = int(parts[1])
        episode = int(parts[2])
    except Exception:
        raise HTTPException(
            status_code=400, detail="Invalid library key (season/episode must be int)"
        ) from None
    if not slug or season < 1 or episode < 1:
        raise HTTPException(status_code=400, detail="Invalid library key (empty or out of range)")
    return slug, season, episode


def _library_key(series_slug: str, season_number: int, episode_number: int) -> str:
    return f"{series_slug}:{int(season_number)}:{int(episode_number)}"


def _jobs_for_key(
    store, *, series_slug: str, season_number: int, episode_number: int
) -> list[dict[str, str]]:
    slug = str(series_slug or "").strip()
    season = int(season_number)
    episode = int(episode_number)
    con = store._conn()
    try:
        rows = con.execute(
            """
            SELECT job_id, owner_user_id, visibility
            FROM job_library
            WHERE series_slug = ?
              AND season_number = ?
              AND episode_number = ?;
            """,
            (slug, int(season), int(episode)),
        ).fetchall()
        return [
            {
                "job_id": str(r["job_id"] or ""),
                "owner_id": str(r["owner_user_id"] or ""),
                "visibility": str(r["visibility"] or "private"),
            }
            for r in rows
        ]
    finally:
        con.close()


