"""Users, login sessions and per-user settings (#86).

Every store method takes the caller's connection, so a service can combine
several stores in one ``Database.transaction()``. Stores only read and write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    id: int
    username: str
    nd_password_enc: str
    lb_token_enc: str | None
    is_admin: int
    status: str
    created_at: int


@dataclass(frozen=True)
class Session:
    id: int
    user_id: int
    token_hash: str
    created_at: int
    last_seen_at: int
    expires_at: int


class UsersStore:
    def get(self, conn: sqlite3.Connection, user_id: int) -> User | None:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(**row) if row else None

    def first_admin(self, conn: sqlite3.Connection) -> User | None:
        row = conn.execute(
            "SELECT * FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        return User(**row) if row else None

    def get_by_username(self, conn: sqlite3.Connection, username: str) -> User | None:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return User(**row) if row else None

    def upsert_on_login(
        self,
        conn: sqlite3.Connection,
        username: str,
        nd_password_enc: str,
        is_admin: bool,
        now: int,
    ) -> User:
        """Create the user on first login; later logins refresh password and admin flag."""
        row = conn.execute(
            """INSERT INTO users (username, nd_password_enc, is_admin, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (username) DO UPDATE SET
                   nd_password_enc = excluded.nd_password_enc,
                   is_admin = excluded.is_admin,
                   status = 'active'
               RETURNING *""",
            (username, nd_password_enc, int(is_admin), now),
        ).fetchone()
        return User(**row)

    def all(self, conn: sqlite3.Connection) -> list[User]:
        rows = conn.execute("SELECT * FROM users ORDER BY username")
        return [User(**r) for r in rows]

    def set_lb_token(
        self, conn: sqlite3.Connection, user_id: int, lb_token_enc: str | None
    ) -> None:
        conn.execute(
            "UPDATE users SET lb_token_enc = ? WHERE id = ?", (lb_token_enc, user_id)
        )

    def set_status(self, conn: sqlite3.Connection, user_id: int, status: str) -> None:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))

    # ------------------------------------------------------------ sessions

    def create_session(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        token_hash: str,
        now: int,
        expires_at: int,
    ) -> Session:
        row = conn.execute(
            """INSERT INTO sessions (user_id, token_hash, created_at, last_seen_at, expires_at)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (user_id, token_hash, now, now, expires_at),
        ).fetchone()
        return Session(**row)

    def get_session(self, conn: sqlite3.Connection, token_hash: str) -> Session | None:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return Session(**row) if row else None

    def touch_session(
        self, conn: sqlite3.Connection, session_id: int, now: int, expires_at: int
    ) -> None:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
            (now, expires_at, session_id),
        )

    def delete_session(self, conn: sqlite3.Connection, session_id: int) -> None:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_user_sessions(self, conn: sqlite3.Connection, user_id: int) -> int:
        return conn.execute(
            "DELETE FROM sessions WHERE user_id = ?", (user_id,)
        ).rowcount

    def delete_expired_sessions(self, conn: sqlite3.Connection, now: int) -> int:
        return conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (now,)
        ).rowcount

    # ------------------------------------------------------------ settings

    def get_setting(self, conn: sqlite3.Connection, user_id: int, key: str) -> Any:
        """The stored JSON value, or None when unset."""
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def get_settings(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT key, value FROM user_settings WHERE user_id = ?", (user_id,)
        )
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_setting(
        self, conn: sqlite3.Connection, user_id: int, key: str, value: Any, now: int
    ) -> None:
        conn.execute(
            """INSERT INTO user_settings (user_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (user_id, key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (user_id, key, json.dumps(value), now),
        )

    def delete_setting(self, conn: sqlite3.Connection, user_id: int, key: str) -> None:
        conn.execute(
            "DELETE FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key)
        )
