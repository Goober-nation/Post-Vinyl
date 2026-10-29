"""Current file per song and its tagging attempts (#86).

Rows are created by the ``downloads_completed_handoff`` trigger, not here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FileRecord:
    id: int
    song_id: int
    source: str
    file_location: str
    file_state: str
    verification: str | None
    navidrome_id: str | None
    downloaded_at: int
    updated_at: int


@dataclass(frozen=True)
class TaggingRecord:
    id: int
    file_id: int
    status: str
    error: str | None
    mb_recording_id: str | None
    mb_release_id: str | None
    tags_written: dict[str, Any] | None
    tagged_at: int


def _tagging(row: sqlite3.Row) -> TaggingRecord:
    fields = dict(row)
    tags = fields.pop("tags_written")
    return TaggingRecord(**fields, tags_written=json.loads(tags) if tags else None)


class FilesStore:
    def get(self, conn: sqlite3.Connection, file_id: int) -> FileRecord | None:
        row = conn.execute(
            "SELECT * FROM file_management WHERE id = ?", (file_id,)
        ).fetchone()
        return FileRecord(**row) if row else None

    def for_song(self, conn: sqlite3.Connection, song_id: int) -> FileRecord | None:
        row = conn.execute(
            "SELECT * FROM file_management WHERE song_id = ?", (song_id,)
        ).fetchone()
        return FileRecord(**row) if row else None

    def by_navidrome_ids(
        self, conn: sqlite3.Connection, navidrome_ids: list[str]
    ) -> dict[str, FileRecord]:
        """Our records for library songs, by Navidrome id (#112); a hand-added
        song has none."""
        if not navidrome_ids:
            return {}
        marks = ",".join("?" * len(navidrome_ids))
        rows = conn.execute(
            f"SELECT * FROM file_management WHERE navidrome_id IN ({marks})",
            navidrome_ids,
        )
        return {r["navidrome_id"]: FileRecord(**r) for r in rows}

    def in_state(self, conn: sqlite3.Connection, file_state: str) -> list[FileRecord]:
        rows = conn.execute(
            "SELECT * FROM file_management WHERE file_state = ? ORDER BY id",
            (file_state,),
        )
        return [FileRecord(**r) for r in rows]

    def set_state(
        self, conn: sqlite3.Connection, file_id: int, file_state: str, now: int
    ) -> None:
        """Plain write; allowed transitions are core/states.py (milestone 5)."""
        conn.execute(
            "UPDATE file_management SET file_state = ?, updated_at = ? WHERE id = ?",
            (file_state, now, file_id),
        )

    def set_verification(
        self, conn: sqlite3.Connection, file_id: int, verification: str, now: int
    ) -> None:
        conn.execute(
            "UPDATE file_management SET verification = ?, updated_at = ? WHERE id = ?",
            (verification, now, file_id),
        )

    def set_location(
        self, conn: sqlite3.Connection, file_id: int, file_location: str, now: int
    ) -> None:
        conn.execute(
            "UPDATE file_management SET file_location = ?, updated_at = ? WHERE id = ?",
            (file_location, now, file_id),
        )

    def set_navidrome_id(
        self, conn: sqlite3.Connection, file_id: int, navidrome_id: str | None, now: int
    ) -> None:
        conn.execute(
            "UPDATE file_management SET navidrome_id = ?, updated_at = ? WHERE id = ?",
            (navidrome_id, now, file_id),
        )

    # ------------------------------------------------------------ tagging

    def add_tagging(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        status: str,
        now: int,
        *,
        error: str | None = None,
        mb_recording_id: str | None = None,
        mb_release_id: str | None = None,
        tags_written: dict[str, Any] | None = None,
    ) -> TaggingRecord:
        row = conn.execute(
            """INSERT INTO tagging (file_id, status, error, mb_recording_id,
                                    mb_release_id, tags_written, tagged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (
                file_id,
                status,
                error,
                mb_recording_id,
                mb_release_id,
                json.dumps(tags_written) if tags_written is not None else None,
                now,
            ),
        ).fetchone()
        return _tagging(row)

    def tagging_for_file(
        self, conn: sqlite3.Connection, file_id: int
    ) -> list[TaggingRecord]:
        rows = conn.execute(
            "SELECT * FROM tagging WHERE file_id = ? ORDER BY id", (file_id,)
        )
        return [_tagging(r) for r in rows]
