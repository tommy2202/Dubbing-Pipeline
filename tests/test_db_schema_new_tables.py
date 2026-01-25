from __future__ import annotations

from pathlib import Path

from dubbing_pipeline.jobs.models import Job, JobState, now_utc
from dubbing_pipeline.jobs.store import JobStore


def _table_columns(con, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    return {str(r["name"]) for r in rows}


def _table_indexes(con, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA index_list({table});").fetchall()
    return {str(r["name"]) for r in rows}


def _mk_job(job_id: str) -> Job:
    now = now_utc()
    return Job(
        id=job_id,
        owner_id="u1",
        video_path="Input/example.mp4",
        duration_s=10.0,
        mode="medium",
        device="cpu",
        src_lang="auto",
        tgt_lang="en",
        created_at=now,
        updated_at=now,
        state=JobState.QUEUED,
        progress=0.0,
        message="Queued",
        output_mkv="",
        output_srt="",
        work_dir="",
        log_path="",
        error=None,
        request_id="r1",
    )


def test_new_schema_tables_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    _ = JobStore(db_path)

    con = JobStore(db_path)._conn()
    try:
        tables = {
            str(r["name"])
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        assert "qa_reviews" in tables
        assert "voice_profiles" in tables
        assert "voice_profile_aliases" in tables
        assert "glossaries" in tables
        assert "pronunciation_dict" in tables

        qa_cols = _table_columns(con, "qa_reviews")
        assert {
            "id",
            "job_id",
            "segment_id",
            "status",
            "notes",
            "edited_text",
            "pronunciation_overrides",
            "glossary_used",
            "created_by",
            "created_at",
            "updated_at",
        }.issubset(qa_cols)

        vp_cols = _table_columns(con, "voice_profiles")
        assert {
            "id",
            "display_name",
            "created_by",
            "created_at",
            "scope",
            "series_lock",
            "source_type",
            "export_allowed",
            "share_allowed",
            "reuse_allowed",
            "expires_at",
            "embedding_vector",
            "embedding_model_id",
            "metadata_json",
        }.issubset(vp_cols)

        alias_cols = _table_columns(con, "voice_profile_aliases")
        assert {
            "id",
            "voice_profile_id",
            "alias_of_voice_profile_id",
            "confidence",
            "approved_by_admin",
            "approved_at",
        }.issubset(alias_cols)

        gloss_cols = _table_columns(con, "glossaries")
        assert {
            "id",
            "name",
            "language_pair",
            "priority",
            "enabled",
            "rules_json",
            "created_at",
            "updated_at",
        }.issubset(gloss_cols)

        pron_cols = _table_columns(con, "pronunciation_dict")
        assert {
            "id",
            "lang",
            "term",
            "ipa_or_phoneme",
            "example",
            "created_by",
            "created_at",
        }.issubset(pron_cols)

        qa_indexes = _table_indexes(con, "qa_reviews")
        assert "idx_qa_reviews_job_id" in qa_indexes

        vp_indexes = _table_indexes(con, "voice_profiles")
        assert "idx_voice_profiles_series_lock" in vp_indexes
        assert "idx_voice_profiles_embedding_model" in vp_indexes
    finally:
        con.close()


def test_new_tables_crud(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    store = JobStore(db_path)
    store.put(_mk_job("job_1"))

    con = store._conn()
    try:
        con.execute(
            """
            INSERT INTO qa_reviews (
              id, job_id, segment_id, status, notes, edited_text,
              pronunciation_overrides, glossary_used, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "qa_1",
                "job_1",
                3,
                "approved",
                "ok",
                "edited text",
                '{"term":"pron"}',
                '{"rules":["a->b"]}',
                "u1",
                1.0,
                2.0,
            ),
        )

        con.execute(
            """
            INSERT INTO voice_profiles (
              id, display_name, created_by, created_at, scope, series_lock, source_type,
              export_allowed, share_allowed, reuse_allowed, embedding_vector, embedding_model_id,
              metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "vp_user_1",
                "User Upload",
                "u1",
                10.0,
                "private",
                None,
                "user_upload",
                0,
                0,
                None,
                b"\x01\x02",
                "emb_v1",
                '{"notes":"uploaded"}',
            ),
        )

        con.execute(
            """
            INSERT INTO voice_profiles (
              id, display_name, created_by, created_at, scope, series_lock, source_type,
              export_allowed, share_allowed, reuse_allowed, embedding_vector, embedding_model_id,
              metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "vp_media_1",
                "Extracted",
                "u2",
                11.0,
                "private",
                "series_a",
                "extracted_from_media",
                0,
                0,
                None,
                None,
                "emb_v1",
                '{"provenance":"job"}',
            ),
        )

        con.execute(
            """
            INSERT INTO voice_profile_aliases (
              id, voice_profile_id, alias_of_voice_profile_id, confidence, approved_by_admin, approved_at
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            ("alias_1", "vp_user_1", "vp_media_1", 0.82, 1, 12.0),
        )

        con.execute(
            """
            INSERT INTO glossaries (
              id, name, language_pair, priority, enabled, rules_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            ("gl_1", "Main", "en->ja", 1, 1, '{"map":{"A":"B"}}', 20.0, 21.0),
        )

        con.execute(
            """
            INSERT INTO pronunciation_dict (
              id, lang, term, ipa_or_phoneme, example, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            ("pd_1", "en", "Kobayashi", "ko-ba-ya-shi", "Example", "u1", 30.0),
        )

        con.commit()

        qa = con.execute(
            "SELECT id, job_id, segment_id, status, edited_text FROM qa_reviews WHERE id = ?;",
            ("qa_1",),
        ).fetchone()
        assert qa is not None
        assert qa["job_id"] == "job_1"
        assert int(qa["segment_id"]) == 3
        assert qa["status"] == "approved"
        assert qa["edited_text"] == "edited text"

        vp_user = con.execute(
            "SELECT reuse_allowed FROM voice_profiles WHERE id = ?;",
            ("vp_user_1",),
        ).fetchone()
        assert vp_user is not None
        assert int(vp_user["reuse_allowed"]) == 1

        vp_media = con.execute(
            "SELECT reuse_allowed, export_allowed, share_allowed FROM voice_profiles WHERE id = ?;",
            ("vp_media_1",),
        ).fetchone()
        assert vp_media is not None
        assert int(vp_media["reuse_allowed"]) == 0
        assert int(vp_media["export_allowed"]) == 0
        assert int(vp_media["share_allowed"]) == 0

        alias = con.execute(
            "SELECT voice_profile_id, alias_of_voice_profile_id FROM voice_profile_aliases WHERE id = ?;",
            ("alias_1",),
        ).fetchone()
        assert alias is not None
        assert alias["voice_profile_id"] == "vp_user_1"
        assert alias["alias_of_voice_profile_id"] == "vp_media_1"

        gl = con.execute(
            "SELECT name, language_pair FROM glossaries WHERE id = ?;",
            ("gl_1",),
        ).fetchone()
        assert gl is not None
        assert gl["name"] == "Main"
        assert gl["language_pair"] == "en->ja"

        pron = con.execute(
            "SELECT term, ipa_or_phoneme FROM pronunciation_dict WHERE id = ?;",
            ("pd_1",),
        ).fetchone()
        assert pron is not None
        assert pron["term"] == "Kobayashi"
        assert pron["ipa_or_phoneme"] == "ko-ba-ya-shi"
    finally:
        con.close()
