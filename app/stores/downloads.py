"""Generic downloads: one row per file wanted, whatever source fetches it (#86)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Download:
    id: int
    request_id: int
    source: str
    state: str
    file_location: str | None
    created_at: int
    completed_at: int | None


@dataclass(frozen=True)
class QueueRow:
    """A download with what the downloads tab shows next to it (#111)."""

    id: int
    request_id: int
    user_id: int
    username: str
    source: str
    state: str
    created_at: int
    completed_at: int | None
    track_state: str
    title: str | None
    artist: str | None


_QUEUE_SQL = """
    FROM downloads d
    JOIN requests r ON r.id = d.request_id
    JOIN users u ON u.id = r.user_id
    LEFT JOIN songs s ON s.id = r.song_id
    WHERE (:user IS NULL OR r.user_id = :user) AND (:state IS NULL OR d.state = :state)
      AND (:id IS NULL OR d.id = :id)"""


class DownloadsStore:
    def get(self, conn: sqlite3.Connection, download_id: int) -> Download | None:
        row = conn.execute(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        ).fetchone()
        return Download(**row) if row else None

    def create(
        self, conn: sqlite3.Connection, request_id: int, source: str, now: int
    ) -> Download:
        row = conn.execute(
            """INSERT INTO downloads (request_id, source, created_at)
               VALUES (?, ?, ?) RETURNING *""",
            (request_id, source, now),
        ).fetchone()
        return Download(**row)

    def set_state(self, conn: sqlite3.Connection, download_id: int, state: str) -> None:
        """Any state but completed (that's ``complete``)."""
        if state == "completed":
            raise ValueError("use complete() so the file location is set")
        conn.execute(
            "UPDATE downloads SET state = ? WHERE id = ?", (state, download_id)
        )

    def complete(
        self, conn: sqlite3.Connection, download_id: int, file_location: str, now: int
    ) -> None:
        """Marks it done; the DB trigger hands the file to file_management."""
        conn.execute(
            """UPDATE downloads SET state = 'completed', file_location = ?, completed_at = ?
               WHERE id = ?""",
            (file_location, now, download_id),
        )

    def for_request(self, conn: sqlite3.Connection, request_id: int) -> list[Download]:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE request_id = ? ORDER BY id", (request_id,)
        )
        return [Download(**r) for r in rows]

    def in_state(
        self, conn: sqlite3.Connection, state: str, source: str | None = None
    ) -> list[Download]:
        rows = conn.execute(
            """SELECT * FROM downloads WHERE state = ? AND (? IS NULL OR source = ?)
               ORDER BY id""",
            (state, source, source),
        )
        return [Download(**r) for r in rows]

    def queue_page(
        self,
        conn: sqlite3.Connection,
        user_id: int | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> list[QueueRow]:
        """Newest first; ``user_id`` None = every user's."""
        rows = conn.execute(
            f"""SELECT d.id, d.request_id, r.user_id, u.username, d.source, d.state,
                       d.created_at, d.completed_at, r.track_state,
                       s.title, s.artist_name AS artist
                {_QUEUE_SQL} ORDER BY d.id DESC LIMIT :limit OFFSET :offset""",
            {"user": user_id, "state": state, "id": None, "limit": limit,
             "offset": offset},
        )  # fmt: skip
        return [QueueRow(**r) for r in rows]

    def queue_row(self, conn: sqlite3.Connection, download_id: int) -> QueueRow | None:
        row = conn.execute(
            f"""SELECT d.id, d.request_id, r.user_id, u.username, d.source, d.state,
                       d.created_at, d.completed_at, r.track_state,
                       s.title, s.artist_name AS artist
                {_QUEUE_SQL}""",
            {"user": None, "state": None, "id": download_id},
        ).fetchone()
        return QueueRow(**row) if row else None

    def queue_count(
        self, conn: sqlite3.Connection, user_id: int | None, state: str | None
    ) -> int:
        row = conn.execute(
            f"SELECT COUNT(*) {_QUEUE_SQL}",
            {"user": user_id, "state": state, "id": None},
        ).fetchone()
        return int(row[0])
