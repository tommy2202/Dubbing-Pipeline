from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Role(str, Enum):
    admin = "admin"
    operator = "operator"
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
        self._init()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
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
            con.commit()
        finally:
            con.close()

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


def now_ts() -> int:
    return int(time.time())
