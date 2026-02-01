from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def parse_library_metadata_or_422(payload: dict[str, Any]) -> tuple[str, str, int, int]:
    """
    Parse required library metadata for job submission.
    Backwards-compatible for old persisted jobs, but NEW submissions must include:
      - series_title (non-empty)
      - season (parseable int >= 1)
      - episode (parseable int >= 1)
    """
    from dubbing_pipeline.library.normalize import normalize_series_title, parse_int_strict, series_to_slug

    series_title = normalize_series_title(str(payload.get("series_title") or ""))
    if not series_title:
        raise HTTPException(status_code=422, detail="series_title is required")
    slug = str(payload.get("series_slug") or "").strip()
    if not slug:
        slug = series_to_slug(series_title)
    if not slug:
        raise HTTPException(status_code=422, detail="series_title is invalid (cannot derive slug)")

    # Accept either *_number or *_text (UI sends text).
    season_in = payload.get("season_number")
    if season_in is None:
        season_in = payload.get("season_text")
    if season_in is None:
        season_in = payload.get("season")
    episode_in = payload.get("episode_number")
    if episode_in is None:
        episode_in = payload.get("episode_text")
    if episode_in is None:
        episode_in = payload.get("episode")

    try:
        season_number = parse_int_strict(season_in, "season_number")
    except ValueError as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from None
    try:
        episode_number = parse_int_strict(episode_in, "episode_number")
    except ValueError as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from None

    return series_title, slug, int(season_number), int(episode_number)
