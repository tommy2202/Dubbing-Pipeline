from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from dubbing_pipeline.utils.locks import file_lock


class Role(str, Enum):
    admin = "admin"
    operator = "operator"
    editor = "editor"
    viewer = "viewer"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    password_hash: str
    role: Role
    totp_secret: str | None
    totp_enabled: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: str
    prefix: str
    key_hash: str
    scopes_json: str
    user_id: str
    created_at: int
    revoked: bool

    @property
    def scopes(self) -> list[str]:
        try:
            v = json.loads(self.scopes_json or "[]")
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []


class AuthStore:
    """
    Minimal SQLite-backed store for users + API keys.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self._init()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _write_lock(self):
        return file_lock(self._lock_path)

    def _init(self) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                      id TEXT PRIMARY KEY,
                      username TEXT UNIQUE NOT NULL,
                      password_hash TEXT NOT NULL,
                      role TEXT NOT NULL,
                      totp_secret TEXT,
                      totp_enabled INTEGER NOT NULL,
                      created_at INTEGER NOT NULL
                    );
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_keys (
                      id TEXT PRIMARY KEY,
                      prefix TEXT NOT NULL,
                      key_hash TEXT NOT NULL,
                      scopes_json TEXT NOT NULL,
                      user_id TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      revoked INTEGER NOT NULL,
                      FOREIGN KEY(user_id) REFERENCES users(id)
                    );
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS api_keys_prefix ON api_keys(prefix);")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refresh_tokens (
                      jti TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      token_hash TEXT NOT NULL,
                      created_at INTEGER NOT NULL,
                      expires_at INTEGER NOT NULL,
                      revoked INTEGER NOT NULL,
                      replaced_by TEXT,
                      last_used_at INTEGER,
                      device_id TEXT,
                      device_name TEXT,
                      created_ip TEXT,
                      last_ip TEXT,
                      user_agent TEXT,
                      FOREIGN KEY(user_id) REFERENCES users(id)
                    );
                    """
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS refresh_tokens_user_id ON refresh_tokens(user_id);"
                )
                # Best-effort schema migration for older DBs (SQLite).
                self._ensure_refresh_token_columns(con)
                self._ensure_qr_tables(con)
                self._ensure_recovery_tables(con)
                self._ensure_invite_tables(con)
                con.commit()
            finally:
                con.close()

    def _ensure_refresh_token_columns(self, con: sqlite3.Connection) -> None:
        """
        Add new columns to refresh_tokens if missing (best-effort, idempotent).
        """
        try:
            cols = [
                str(r["name"]) for r in con.execute("PRAGMA table_info(refresh_tokens);").fetchall()
            ]
        except Exception:
            return
        want = {
            "device_id": "TEXT",
            "device_name": "TEXT",
            "created_ip": "TEXT",
            "last_ip": "TEXT",
            "user_agent": "TEXT",
        }
        for name, typ in want.items():
            if name in cols:
                continue
            try:
                con.execute(f"ALTER TABLE refresh_tokens ADD COLUMN {name} {typ};")
            except Exception:
                continue

    def _ensure_qr_tables(self, con: sqlite3.Connection) -> None:
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS qr_login_codes (
                  nonce_hash TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  used_at INTEGER,
                  created_ip TEXT,
                  used_ip TEXT
                );
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS qr_login_codes_user_id ON qr_login_codes(user_id);"
            )
        except Exception:
            return

    def _ensure_recovery_tables(self, con: sqlite3.Connection) -> None:
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS totp_recovery_codes (
                  user_id TEXT NOT NULL,
                  code_hash TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  used_at INTEGER,
                  PRIMARY KEY(user_id, code_hash)
                );
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS totp_recovery_codes_user_id ON totp_recovery_codes(user_id);"
            )
        except Exception:
            return

    def _ensure_invite_tables(self, con: sqlite3.Connection) -> None:
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS invites (
                  token_hash TEXT PRIMARY KEY,
                  created_by TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  expires_at INTEGER NOT NULL,
                  used_at INTEGER,
                  used_by TEXT
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS invites_created_by ON invites(created_by);")
            con.execute("CREATE INDEX IF NOT EXISTS invites_used_by ON invites(used_by);")
        except Exception:
            return

    def get_user_by_username(self, username: str) -> User | None:
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return None
            return User(
                id=str(row["id"]),
                username=str(row["username"]),
                password_hash=str(row["password_hash"]),
                role=Role(str(row["role"])),
                totp_secret=(str(row["totp_secret"]) if row["totp_secret"] is not None else None),
                totp_enabled=bool(int(row["totp_enabled"])),
                created_at=int(row["created_at"]),
            )
        finally:
            con.close()

    def get_user(self, user_id: str) -> User | None:
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            return User(
                id=str(row["id"]),
                username=str(row["username"]),
                password_hash=str(row["password_hash"]),
                role=Role(str(row["role"])),
                totp_secret=(str(row["totp_secret"]) if row["totp_secret"] is not None else None),
                totp_enabled=bool(int(row["totp_enabled"])),
                created_at=int(row["created_at"]),
            )
        finally:
            con.close()

    def upsert_user(self, user: User) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role, totp_secret, totp_enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                      password_hash=excluded.password_hash,
                      role=excluded.role,
                      totp_secret=excluded.totp_secret,
                      totp_enabled=excluded.totp_enabled
                    """,
                    (
                        user.id,
                        user.username,
                        user.password_hash,
                        user.role.value,
                        user.totp_secret,
                        1 if user.totp_enabled else 0,
                        user.created_at,
                    ),
                )
                con.commit()
            finally:
                con.close()

    def set_totp(self, user_id: str, *, secret: str | None, enabled: bool) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    "UPDATE users SET totp_secret=?, totp_enabled=? WHERE id=?",
                    (secret, 1 if enabled else 0, user_id),
                )
                con.commit()
            finally:
                con.close()

    def create_api_key(self, api_key: ApiKey) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO api_keys (id, prefix, key_hash, scopes_json, user_id, created_at, revoked)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        api_key.id,
                        api_key.prefix,
                        api_key.key_hash,
                        api_key.scopes_json,
                        api_key.user_id,
                        api_key.created_at,
                        1 if api_key.revoked else 0,
                    ),
                )
                con.commit()
            finally:
                con.close()

    def list_api_keys(self, user_id: str | None = None) -> list[ApiKey]:
        con = self._conn()
        try:
            if user_id:
                rows = con.execute(
                    "SELECT * FROM api_keys WHERE user_id=? ORDER BY created_at DESC", (user_id,)
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
            out = []
            for r in rows:
                out.append(
                    ApiKey(
                        id=str(r["id"]),
                        prefix=str(r["prefix"]),
                        key_hash=str(r["key_hash"]),
                        scopes_json=str(r["scopes_json"]),
                        user_id=str(r["user_id"]),
                        created_at=int(r["created_at"]),
                        revoked=bool(int(r["revoked"])),
                    )
                )
            return out
        finally:
            con.close()

    def revoke_api_key(self, key_id: str) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (key_id,))
                con.commit()
            finally:
                con.close()

    def find_api_keys_by_prefix(self, prefix: str) -> list[ApiKey]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT * FROM api_keys WHERE prefix=? AND revoked=0", (prefix,)
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    ApiKey(
                        id=str(r["id"]),
                        prefix=str(r["prefix"]),
                        key_hash=str(r["key_hash"]),
                        scopes_json=str(r["scopes_json"]),
                        user_id=str(r["user_id"]),
                        created_at=int(r["created_at"]),
                        revoked=bool(int(r["revoked"])),
                    )
                )
            return out
        finally:
            con.close()

    # --- rotating refresh tokens (server-side) ---

    def put_refresh_token(
        self,
        *,
        jti: str,
        user_id: str,
        token_hash: str,
        created_at: int,
        expires_at: int,
        device_id: str | None = None,
        device_name: str | None = None,
        created_ip: str | None = None,
        last_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                # Prefer inserting with extended device/session metadata; fall back to legacy schema if needed.
                try:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO refresh_tokens
                          (jti, user_id, token_hash, created_at, expires_at, revoked, replaced_by, last_used_at,
                           device_id, device_name, created_ip, last_ip, user_agent)
                        VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(jti),
                            str(user_id),
                            str(token_hash),
                            int(created_at),
                            int(expires_at),
                            str(device_id or "") or None,
                            str(device_name or "") or None,
                            str(created_ip or "") or None,
                            str(last_ip or "") or None,
                            (str(user_agent)[:160] if user_agent else None),
                        ),
                    )
                except Exception:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO refresh_tokens
                          (jti, user_id, token_hash, created_at, expires_at, revoked, replaced_by, last_used_at)
                        VALUES (?, ?, ?, ?, ?, 0, NULL, NULL)
                        """,
                        (str(jti), str(user_id), str(token_hash), int(created_at), int(expires_at)),
                    )
                    # Best-effort metadata update if columns exist.
                    with __import__("contextlib").suppress(Exception):
                        con.execute(
                            """
                            UPDATE refresh_tokens
                            SET device_id=?, device_name=?, created_ip=?, last_ip=?, user_agent=?
                            WHERE jti=?
                            """,
                            (
                                str(device_id or "") or None,
                                str(device_name or "") or None,
                                str(created_ip or "") or None,
                                str(last_ip or "") or None,
                                (str(user_agent)[:160] if user_agent else None),
                                str(jti),
                            ),
                        )
                con.commit()
            finally:
                con.close()

    def get_refresh_token(self, jti: str) -> dict[str, object] | None:
        con = self._conn()
        try:
            row = con.execute("SELECT * FROM refresh_tokens WHERE jti=?", (jti,)).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            con.close()

    def rotate_refresh_token(self, *, old_jti: str, new_jti: str) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    UPDATE refresh_tokens
                    SET revoked=1, replaced_by=?, last_used_at=?
                    WHERE jti=?
                    """,
                    (new_jti, now_ts(), old_jti),
                )
                con.commit()
            finally:
                con.close()

    def revoke_refresh_token(self, jti: str) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    "UPDATE refresh_tokens SET revoked=1, last_used_at=? WHERE jti=?",
                    (now_ts(), jti),
                )
                con.commit()
            finally:
                con.close()

    def revoke_all_refresh_tokens_for_user(self, user_id: str) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    "UPDATE refresh_tokens SET revoked=1, last_used_at=? WHERE user_id=?",
                    (now_ts(), user_id),
                )
                con.commit()
            finally:
                con.close()

    def list_active_sessions(self, *, user_id: str) -> list[dict[str, Any]]:
        """
        "Sessions" are active (non-revoked) refresh-token chains.
        """
        con = self._conn()
        try:
            now = int(time.time())
            rows = con.execute(
                """
                SELECT * FROM refresh_tokens
                WHERE user_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at>?)
                ORDER BY created_at DESC
                """,
                (str(user_id), int(now)),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def revoke_sessions_by_device(self, *, user_id: str, device_id: str) -> int:
        with self._write_lock():
            con = self._conn()
            try:
                cur = con.execute(
                    "UPDATE refresh_tokens SET revoked=1, last_used_at=? WHERE user_id=? AND device_id=?",
                    (now_ts(), str(user_id), str(device_id)),
                )
                con.commit()
                return int(getattr(cur, "rowcount", 0) or 0)
            finally:
                con.close()

    # --- invite tokens (invite-only access) ---

    def create_invite(
        self,
        *,
        token_hash: str,
        created_by: str,
        created_at: int,
        expires_at: int,
    ) -> dict[str, object]:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO invites (token_hash, created_by, created_at, expires_at, used_at, used_by)
                    VALUES (?, ?, ?, ?, NULL, NULL)
                    """,
                    (str(token_hash), str(created_by), int(created_at), int(expires_at)),
                )
                con.commit()
            finally:
                con.close()
        return {
            "token_hash": str(token_hash),
            "created_by": str(created_by),
            "created_at": int(created_at),
            "expires_at": int(expires_at),
            "used_at": None,
            "used_by": None,
        }

    def list_invites(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, object]]:
        lim = max(1, min(500, int(limit)))
        off = max(0, int(offset))
        con = self._conn()
        try:
            rows = con.execute(
                """
                SELECT token_hash, created_by, created_at, expires_at, used_at, used_by
                FROM invites
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?;
                """,
                (lim, off),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def _invite_row(self, *, con: sqlite3.Connection, token_hash: str) -> dict[str, object] | None:
        row = con.execute(
            """
            SELECT token_hash, created_by, created_at, expires_at, used_at, used_by
            FROM invites
            WHERE token_hash = ?
            LIMIT 1;
            """,
            (str(token_hash),),
        ).fetchone()
        return dict(row) if row is not None else None

    def create_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: Role,
        created_at: int,
    ) -> User:
        from dubbing_pipeline.utils.crypto import random_id

        user_id = random_id("u_", 16)
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role, totp_secret, totp_enabled, created_at)
                    VALUES (?, ?, ?, ?, NULL, 0, ?)
                    """,
                    (str(user_id), str(username), str(password_hash), str(role.value), int(created_at)),
                )
                con.commit()
                return User(
                    id=str(user_id),
                    username=str(username),
                    password_hash=str(password_hash),
                    role=role,
                    totp_secret=None,
                    totp_enabled=False,
                    created_at=int(created_at),
                )
            finally:
                con.close()

    def redeem_invite(
        self,
        *,
        token_hash: str,
        username: str,
        password_hash: str,
        role: Role,
    ) -> tuple[User | None, str, dict[str, object] | None]:
        """
        Redeem an invite token:
        - validate token (exists, not used, not expired)
        - create user with provided credentials
        - mark invite used

        Returns (user, status, invite_row)
        status: ok|not_found|used|expired|username_taken|invalid
        """
        now = now_ts()
        with self._write_lock():
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE;")
                invite = self._invite_row(con=con, token_hash=token_hash)
                if invite is None:
                    return None, "not_found", None
                if invite.get("used_at"):
                    return None, "used", invite
                exp = int(invite.get("expires_at") or 0)
                if exp and int(now) > exp:
                    return None, "expired", invite

                # Ensure username is unique
                row = con.execute(
                    "SELECT 1 FROM users WHERE username = ? LIMIT 1;", (str(username),)
                ).fetchone()
                if row is not None:
                    return None, "username_taken", invite

                from dubbing_pipeline.utils.crypto import random_id

                user_id = random_id("u_", 16)
                con.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role, totp_secret, totp_enabled, created_at)
                    VALUES (?, ?, ?, ?, NULL, 0, ?)
                    """,
                    (str(user_id), str(username), str(password_hash), str(role.value), int(now)),
                )
                con.execute(
                    """
                    UPDATE invites
                    SET used_at = ?, used_by = ?
                    WHERE token_hash = ?;
                    """,
                    (int(now), str(user_id), str(token_hash)),
                )
                con.commit()
                user = User(
                    id=str(user_id),
                    username=str(username),
                    password_hash=str(password_hash),
                    role=role,
                    totp_secret=None,
                    totp_enabled=False,
                    created_at=int(now),
                )
                return user, "ok", invite
            finally:
                con.close()

    def put_qr_code(
        self, *, nonce_hash: str, user_id: str, created_at: int, expires_at: int, created_ip: str
    ) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO qr_login_codes (nonce_hash, user_id, created_at, expires_at, used_at, created_ip, used_ip)
                    VALUES (?, ?, ?, ?, NULL, ?, NULL)
                    """,
                    (str(nonce_hash), str(user_id), int(created_at), int(expires_at), str(created_ip)),
                )
                con.commit()
            finally:
                con.close()

    def consume_qr_code(self, *, nonce_hash: str, used_ip: str) -> str | None:
        """
        Mark nonce as used if valid and return user_id. Returns None if invalid/expired/used.
        """
        with self._write_lock():
            con = self._conn()
            try:
                now = int(time.time())
                row = con.execute(
                    "SELECT * FROM qr_login_codes WHERE nonce_hash=?",
                    (str(nonce_hash),),
                ).fetchone()
                if row is None:
                    return None
                rec = dict(row)
                if rec.get("used_at"):
                    return None
                exp = int(rec.get("expires_at") or 0)
                if exp and now > exp:
                    return None
                con.execute(
                    "UPDATE qr_login_codes SET used_at=?, used_ip=? WHERE nonce_hash=?",
                    (now_ts(), str(used_ip), str(nonce_hash)),
                )
                con.commit()
                return str(rec.get("user_id") or "") or None
            finally:
                con.close()

    def put_recovery_codes(self, *, user_id: str, code_hashes: list[str]) -> None:
        with self._write_lock():
            con = self._conn()
            try:
                ts = now_ts()
                for h in code_hashes:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO totp_recovery_codes (user_id, code_hash, created_at, used_at)
                        VALUES (?, ?, ?, NULL)
                        """,
                        (str(user_id), str(h), int(ts)),
                    )
                con.commit()
            finally:
                con.close()

    def consume_recovery_code(self, *, user_id: str, code_hash: str) -> bool:
        with self._write_lock():
            con = self._conn()
            try:
                row = con.execute(
                    """
                    SELECT * FROM totp_recovery_codes
                    WHERE user_id=? AND code_hash=? AND used_at IS NULL
                    """,
                    (str(user_id), str(code_hash)),
                ).fetchone()
                if row is None:
                    return False
                con.execute(
                    "UPDATE totp_recovery_codes SET used_at=? WHERE user_id=? AND code_hash=?",
                    (now_ts(), str(user_id), str(code_hash)),
                )
                con.commit()
                return True
            finally:
                con.close()


def now_ts() -> int:
    return int(time.time())
