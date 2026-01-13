from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from dubbing_pipeline.jobs.models import Job, JobState, Visibility, new_id, now_utc
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.library.normalize import normalize_series_title, parse_int_strict, series_to_slug


def _create_job(
    *,
    owner_id: str,
    series_title: str,
    season: object,
    episode: object,
    visibility: str = "private",
) -> Job:
    created = now_utc()
    title = normalize_series_title(series_title)
    season_i = parse_int_strict(season, "season_number")
    ep_i = parse_int_strict(episode, "episode_number")
    slug = series_to_slug(title)
    vis = str(visibility or "private").strip().lower()
    if vis not in {"private", "public"}:
        vis = "private"
    return Job(
        id=new_id(),
        owner_id=owner_id,
        video_path="/dev/null",
        duration_s=0.0,
        mode="low",
        device="cpu",
        src_lang="ja",
        tgt_lang="en",
        created_at=created,
        updated_at=created,
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
        # library fields
        series_title=title,
        series_slug=slug,
        season_number=int(season_i),
        episode_number=int(ep_i),
        visibility=(Visibility.public if vis == "public" else Visibility.private),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "jobs.db"

        # Create DB + apply migrations (constructor runs schema init).
        store = JobStore(db_path)

        # Insert sample jobs (two owners, one series).
        j1 = _create_job(owner_id="u_alice", series_title="My Show", season="S1", episode="01")
        j2 = _create_job(owner_id="u_alice", series_title="My Show", season="Season 1", episode="2")
        j3 = _create_job(owner_id="u_bob", series_title="My Show", season=1, episode="Ep 03")
        store.put(j1)
        store.put(j2)
        store.put(j3)

        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            slug = series_to_slug("My Show")

            rows = con.execute(
                """
                SELECT job_id, season_number, episode_number
                FROM job_library
                WHERE series_slug = ?
                ORDER BY season_number ASC, episode_number ASC;
                """,
                (slug,),
            ).fetchall()
            got = [(str(r["job_id"]), int(r["season_number"]), int(r["episode_number"])) for r in rows]
            want = sorted(
                [(j1.id, 1, 1), (j2.id, 1, 2), (j3.id, 1, 3)], key=lambda x: (x[1], x[2])
            )
            assert got == want, (got, want)

            rows2 = con.execute(
                "SELECT DISTINCT series_slug FROM job_library WHERE owner_user_id = ?;",
                ("u_alice",),
            ).fetchall()
            slugs = sorted({str(r["series_slug"]) for r in rows2 if str(r["series_slug"])})
            assert slugs == [slug], slugs

            # Ensure required indexes exist (names are part of the migration contract).
            idx = con.execute("PRAGMA index_list(job_library);").fetchall()
            idx_names = {str(r["name"]) for r in idx}
            for required in {
                "idx_job_library_series_slug",
                "idx_job_library_series_season_episode",
                "idx_job_library_owner_user_id",
            }:
                assert required in idx_names, (required, sorted(idx_names))
        finally:
            con.close()

    print("verify_db_migration_library: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

