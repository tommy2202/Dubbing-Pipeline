from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from dubbing_pipeline.api.deps import Identity
from dubbing_pipeline.api.models import Role
from dubbing_pipeline.jobs.store import JobStore


@dataclass(frozen=True, slots=True)
class Page:
    limit: int
    offset: int


def _page(limit: int | None, offset: int | None, *, max_limit: int = 200) -> Page:
    lim = int(limit or 50)
    off = int(offset or 0)
    lim = max(1, min(max_limit, lim))
    off = max(0, off)
    return Page(limit=lim, offset=off)


def _conn(store: JobStore) -> sqlite3.Connection:
    con = sqlite3.connect(str(store.db_path))
    con.row_factory = sqlite3.Row
    return con


def _visibility_where(*, ident: Identity) -> tuple[str, list[Any], str]:
    """
    Returns (sql_where_fragment, params, debug_label).
    """
    if ident.user.role == Role.admin:
        return "1=1", [], "admin_all"
    # Object-level auth: owner-only (admin handled above).
    return "owner_user_id = ?", [str(ident.user.id)], "owner_only"


def _visibility_where_with_view(*, ident: Identity, view: str | None) -> tuple[str, list[Any], str]:
    """
    Visibility filter with an optional 'view' selector used by UI toggles.

    view:
      - all (default): owner_or_public (or admin_all)
      - mine: only owner's items
      - public: only public items
    """
    v = str(view or "all").strip().lower()
    if ident.user.role == Role.admin:
        return "1=1", [], "admin_all"
    # Owner-only for non-admin regardless of view selector.
    if v in {"pub", "public"}:
        return "owner_user_id = ?", [str(ident.user.id)], "owner_only"
    if v in {"my", "mine", "owner"}:
        return "owner_user_id = ?", [str(ident.user.id)], "owner_only"
    return "owner_user_id = ?", [str(ident.user.id)], "owner_only"


def list_series(
    *,
    store: JobStore,
    ident: Identity,
    limit: int | None = None,
    offset: int | None = None,
    order: str | None = None,
    q: str | None = None,
    view: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Returns (items, meta).
    """
    page = _page(limit, offset)
    order_eff = str(order or "title").strip().lower()
    if order_eff not in {"title", "recent"}:
        order_eff = "title"

    vis_where, vis_params, vis_label = _visibility_where_with_view(ident=ident, view=view)

    q_text = str(q or "").strip()
    q_like = f"%{q_text.lower()}%" if q_text else ""
    q_where = ""
    q_params: list[Any] = []
    if q_text:
        q_where = "AND (lower(series_title) LIKE ? OR lower(series_slug) LIKE ?)"
        q_params = [q_like, q_like]

    # Note: series_title may differ between versions; pick the most recently updated title.
    order_sql = (
        "latest_updated_at DESC, series_title COLLATE NOCASE ASC, series_slug ASC"
        if order_eff == "recent"
        else "series_title COLLATE NOCASE ASC, series_slug ASC"
    )

    sql = f"""
    WITH filtered AS (
      SELECT
        series_slug,
        series_title,
        season_number,
        episode_number,
        updated_at
      FROM job_library
      WHERE series_slug IS NOT NULL
        AND series_slug != ''
        AND {vis_where}
        {q_where}
    ),
    latest_title AS (
      SELECT
        series_slug,
        series_title,
        updated_at,
        ROW_NUMBER() OVER (PARTITION BY series_slug ORDER BY updated_at DESC) AS rn
      FROM filtered
    ),
    agg AS (
      SELECT
        series_slug,
        COUNT(DISTINCT season_number) AS seasons_count,
        COUNT(DISTINCT printf('%d:%d', season_number, episode_number)) AS episodes_count,
        MAX(updated_at) AS latest_updated_at
      FROM filtered
      GROUP BY series_slug
    )
    SELECT
      a.series_slug AS series_slug,
      COALESCE(lt.series_title, a.series_slug) AS series_title,
      a.seasons_count AS seasons_count,
      a.episodes_count AS episodes_count,
      a.latest_updated_at AS latest_updated_at
    FROM agg a
    JOIN latest_title lt
      ON lt.series_slug = a.series_slug AND lt.rn = 1
    ORDER BY {order_sql}
    LIMIT ? OFFSET ?;
    """

    con = _conn(store)
    try:
        rows = con.execute(
            sql, [*vis_params, *q_params, page.limit, page.offset]
        ).fetchall()
        items = [
            {
                "series_slug": str(r["series_slug"]),
                "series_title": str(r["series_title"]),
                "seasons_count": int(r["seasons_count"] or 0),
                "episodes_count": int(r["episodes_count"] or 0),
                "latest_updated_at": str(r["latest_updated_at"] or ""),
            }
            for r in rows
        ]
        return items, {
            "limit": page.limit,
            "offset": page.offset,
            "order": order_eff,
            "visibility_filter": vis_label,
            "q": q_text,
        }
    finally:
        con.close()


def list_seasons(
    *,
    store: JobStore,
    ident: Identity,
    series_slug: str,
    limit: int | None = None,
    offset: int | None = None,
    view: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page = _page(limit, offset)
    slug = str(series_slug or "").strip()
    vis_where, vis_params, vis_label = _visibility_where_with_view(ident=ident, view=view)

    sql = f"""
    SELECT
      season_number AS season_number,
      COUNT(DISTINCT episode_number) AS episodes_count
    FROM job_library
    WHERE series_slug = ?
      AND season_number IS NOT NULL
      AND season_number >= 1
      AND {vis_where}
    GROUP BY season_number
    ORDER BY season_number ASC
    LIMIT ? OFFSET ?;
    """
    con = _conn(store)
    try:
        rows = con.execute(sql, [slug, *vis_params, page.limit, page.offset]).fetchall()
        items = [
            {
                "season_number": int(r["season_number"] or 0),
                "episodes_count": int(r["episodes_count"] or 0),
            }
            for r in rows
        ]
        return items, {
            "series_slug": slug,
            "limit": page.limit,
            "offset": page.offset,
            "visibility_filter": vis_label,
        }
    finally:
        con.close()


def list_episodes(
    *,
    store: JobStore,
    ident: Identity,
    series_slug: str,
    season_number: int,
    limit: int | None = None,
    offset: int | None = None,
    episode_number: int | None = None,
    include_versions: bool = False,
    view: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page = _page(limit, offset)
    slug = str(series_slug or "").strip()
    season = int(season_number)
    ep_filter = int(episode_number) if episode_number is not None else None
    include_versions_b = bool(include_versions)
    vis_where, vis_params, vis_label = _visibility_where_with_view(ident=ident, view=view)

    where_ep = ""
    params: list[Any] = [slug, season, *vis_params]
    if ep_filter is not None:
        where_ep = "AND episode_number = ?"
        params.append(ep_filter)

    if include_versions_b:
        sql = f"""
        SELECT
          episode_number,
          job_id,
          visibility,
          created_at,
          updated_at,
          COUNT(*) OVER (PARTITION BY episode_number) AS versions_count
        FROM job_library
        WHERE series_slug = ?
          AND season_number = ?
          AND season_number >= 1
          AND episode_number >= 1
          AND {vis_where}
          {where_ep}
        ORDER BY episode_number ASC, updated_at DESC
        LIMIT ? OFFSET ?;
        """
        params2 = [*params, page.limit, page.offset]
    else:
        sql = f"""
        WITH ranked AS (
          SELECT
            episode_number,
            job_id,
            visibility,
            created_at,
            updated_at,
            ROW_NUMBER() OVER (PARTITION BY episode_number ORDER BY updated_at DESC) AS rn,
            COUNT(*) OVER (PARTITION BY episode_number) AS versions_count
          FROM job_library
          WHERE series_slug = ?
            AND season_number = ?
            AND season_number >= 1
            AND episode_number >= 1
            AND {vis_where}
            {where_ep}
        )
        SELECT episode_number, job_id, visibility, created_at, updated_at, versions_count
        FROM ranked
        WHERE rn = 1
        ORDER BY episode_number ASC
        LIMIT ? OFFSET ?;
        """
        params2 = [*params, page.limit, page.offset]

    con = _conn(store)
    try:
        rows = con.execute(sql, params2).fetchall()
        # Map to API shape, enriching with JobStore state (single source of truth for status).
        items: list[dict[str, Any]] = []
        for r in rows:
            job_id = str(r["job_id"])
            job = store.get(job_id)
            status = ""
            if job is not None:
                st = getattr(job, "state", None)
                status = st.value if hasattr(st, "value") else str(st or "")
            vis = str(r["visibility"] or "private")
            if vis.lower().startswith("visibility."):
                vis = vis.split(".", 1)[1]
            vis = vis.lower()
            if vis not in {"private", "public"}:
                vis = "private"

            # playback_urls: prefer manifest urls if present; otherwise provide the existing job files endpoint.
            playback_urls: dict[str, Any] = {
                "master": None,
                "mobile": None,
                "hls_index": None,
                "files": f"/api/jobs/{job_id}/files",
            }
            if job is not None:
                try:
                    from dubbing_pipeline.library.manifest import read_manifest as _rm
                    from dubbing_pipeline.library.paths import get_library_root_for_job, get_job_output_root

                    cand_paths = [
                        get_library_root_for_job(job) / "manifest.json",
                        get_job_output_root(job) / "manifest.json",
                    ]
                    for mp in cand_paths:
                        man = _rm(mp)
                        if isinstance(man, dict) and isinstance(man.get("urls"), dict):
                            urls = man.get("urls") or {}
                            playback_urls["master"] = urls.get("master")
                            playback_urls["mobile"] = urls.get("mobile")
                            playback_urls["hls_index"] = urls.get("hls_index")
                            break
                except Exception:
                    pass

            epn = int(r["episode_number"] or 0)
            versions_count = int(r["versions_count"] or 1)
            items.append(
                {
                    "episode_number": epn,
                    "job_id": job_id,
                    "status": status,
                    "created_at": str(r["created_at"] or ""),
                    "playback_urls": playback_urls,
                    "visibility": vis,
                    "versions_count": versions_count,
                    "versions_url": (
                        f"/api/library/{slug}/{season}/episodes?episode_number={epn}&include_versions=1"
                    ),
                }
            )

        return items, {
            "series_slug": slug,
            "season_number": season,
            "limit": page.limit,
            "offset": page.offset,
            "include_versions": bool(include_versions_b),
            "episode_number": ep_filter,
            "visibility_filter": vis_label,
        }
    finally:
        con.close()

