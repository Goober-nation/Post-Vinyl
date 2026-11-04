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
    lb_username: str | None
    is_admin: int
    status: str
    created_at: int
    nd_id: str | None = None  # Navidrome's id; NULL until the next login
    deleted_at: int | None = None


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
            """SELECT * FROM users
               WHERE is_admin = 1 AND status = 'active' AND deleted_at IS NULL
               ORDER BY id LIMIT 1"""
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
        nd_id: str | None = None,
    ) -> User:
        """Create the user on first login; later logins refresh name, password
        and admin flag. Navidrome's id finds a renamed user; a row without one
        yet is matched by name. Another row holding the name is stale and
        gives it up."""
        found = None
        if nd_id:
            found = conn.execute(
                "SELECT id FROM users WHERE nd_id = ?", (nd_id,)
            ).fetchone()
            if found is None:
                found = conn.execute(
                    """SELECT id FROM users WHERE username = ? AND nd_id IS NULL
                       AND deleted_at IS NULL""",
                    (username,),
                ).fetchone()
            conn.execute(
                "UPDATE users SET username = username || '~' || id"
                " WHERE username = ? AND id != ?",
                (username, found["id"] if found else 0),
            )
        if found:
            row = conn.execute(
                """UPDATE users SET username = ?, nd_id = ?, nd_password_enc = ?,
                       is_admin = ?, status = 'active'
                   WHERE id = ? RETURNING *""",
                (username, nd_id, nd_password_enc, int(is_admin), found["id"]),
            ).fetchone()
            return User(**row)
        row = conn.execute(
            """INSERT INTO users (username, nd_password_enc, is_admin, created_at, nd_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (username) DO UPDATE SET
                   nd_password_enc = excluded.nd_password_enc,
                   is_admin = excluded.is_admin,
                   status = 'active'
               RETURNING *""",
            (username, nd_password_enc, int(is_admin), now, nd_id),
        ).fetchone()
        return User(**row)

    def all(self, conn: sqlite3.Connection) -> list[User]:
        rows = conn.execute(
            "SELECT * FROM users WHERE deleted_at IS NULL ORDER BY username"
        )
        return [User(**r) for r in rows]

    def set_lb_token(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        lb_token_enc: str | None,
        lb_username: str | None = None,
    ) -> None:
        conn.execute(
            "UPDATE users SET lb_token_enc = ?, lb_username = ? WHERE id = ?",
            (lb_token_enc, lb_username, user_id),
        )

    def set_status(self, conn: sqlite3.Connection, user_id: int, status: str) -> None:
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))

    def set_password(
        self, conn: sqlite3.Connection, user_id: int, nd_password_enc: str
    ) -> None:
        conn.execute(
            "UPDATE users SET nd_password_enc = ?, status = 'active' WHERE id = ?",
            (nd_password_enc, user_id),
        )

    def mark_login_refused(self, conn: sqlite3.Connection, user_id: int) -> None:
        """Navidrome refused the saved login: changed or removed there."""
        conn.execute("UPDATE users SET status = 'needs_reauth' WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def mark_deleted(self, conn: sqlite3.Connection, user_id: int, now: int) -> None:
        """Forget the account; the row stays for the requests that point at it."""
        conn.execute(
            """UPDATE users SET username = 'deleted-' || id, nd_id = NULL,
                   nd_password_enc = '', lb_token_enc = NULL, lb_username = NULL,
                   is_admin = 0, status = 'needs_reauth', deleted_at = ?
               WHERE id = ?""",
            (now, user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))

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
