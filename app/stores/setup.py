"""App-wide key/value state (#86): first-run setup progress, the queue pause (#111)."""

from __future__ import annotations

import sqlite3


class SetupStore:
    def get(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM setup_state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set(self, conn: sqlite3.Connection, key: str, value: str, now: int) -> None:
        conn.execute(
            """INSERT INTO setup_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, now),
        )

    def all(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM setup_state")}
