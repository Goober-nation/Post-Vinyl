"""
FileStore — what happened to each downloaded file (migration 015, #67).

A file_management row is created by the `downloads_completed_handoff`
trigger the moment a download completes — never by app code — and outlives
its download row. App code only moves it through its stages:

    pending ──► moved ──► (navidrome_id linked)
       │          └────► unindexed   (a scan after the move didn't list it)
       └────────────────► missing    (file never appeared on disk)

Navidrome is the source of truth for the file once it is indexed; this
table keeps only where we put it (`placed_path`, relative to the library
root, kept as history) and the link to Navidrome's `media_file.id`.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)


class FileStore:
    """SQLite-backed store for file_management and mb_provenance."""

    def __init__(self, database):
        self._db = database

    def get(self, file_id: int) -> dict | None:
        """A file row plus its MusicBrainz recording id, if any."""
        return self._db.fetch_one(
            "SELECT f.*, p.mb_recording_id FROM file_management f "
            "LEFT JOIN mb_provenance p ON p.file_management_id = f.id WHERE f.id = ?",
            (file_id,),
        )

    def pending(self) -> list[dict]:
        """Files whose download completed but which haven't been placed yet."""
        return self._db.fetch_all(
            "SELECT * FROM file_management WHERE stage = 'pending' ORDER BY id"
        )

    def pending_for(self, username: str, filename: str) -> dict | None:
        """The newest file row for this peer file that is still 'pending'.

        None means there is nothing left for the monitor to place: the file
        was already moved or given up on, or no download completed for it.
        """
        return self._db.fetch_one(
            "SELECT * FROM file_management WHERE username = ? AND filename = ? "
            "AND stage = 'pending' ORDER BY id DESC LIMIT 1",
            (username, filename),
        )

    def mark_moved(self, file_id: int, placed_path: str) -> None:
        """Record a successful move. `placed_path` is relative to the
        library root and includes the final filename."""
        if not placed_path:
            raise ValueError("placed_path must be set when a file is marked moved")
        self._db.execute(
            "UPDATE file_management SET stage = 'moved', placed_path = ? WHERE id = ?",
            (placed_path, file_id),
        )

    def mark_missing(self, file_id: int) -> None:
        """The file never appeared on disk within the timeout; terminal."""
        self._db.execute(
            "UPDATE file_management SET stage = 'missing' WHERE id = ?", (file_id,)
        )

    def unlinked_moved(self, moved_before: int | None = None) -> list[dict]:
        """Moved files not yet linked to a Navidrome song.

        `moved_before` (unix seconds) limits it to files downloaded before
        that time, e.g. before the last completed Navidrome scan.
        """
        if moved_before is None:
            return self._db.fetch_all(
                "SELECT * FROM file_management WHERE stage = 'moved' AND navidrome_id IS NULL"
            )
        return self._db.fetch_all(
            "SELECT * FROM file_management WHERE stage = 'moved' AND navidrome_id IS NULL "
            "AND downloaded_at < ?",
            (moved_before,),
        )

    def set_navidrome_id(self, file_id: int, navidrome_id: str) -> None:
        self._db.execute(
            "UPDATE file_management SET navidrome_id = ? WHERE id = ?",
            (navidrome_id, file_id),
        )

    def mark_unindexed(self, file_id: int) -> None:
        """A Navidrome scan that finished after the move still doesn't list
        the file (rejected, not audio, or deleted); terminal."""
        self._db.execute(
            "UPDATE file_management SET stage = 'unindexed' WHERE id = ?", (file_id,)
        )
        logger.warning("File %d moved but never indexed by Navidrome", file_id)

    def missing_since(self, older_than_seconds: int) -> list[dict]:
        """Pending files whose download completed more than
        `older_than_seconds` ago. Uses the stored downloaded_at, so the
        missing-file timeout survives restarts."""
        cutoff = int(time.time()) - older_than_seconds
        return self._db.fetch_all(
            "SELECT * FROM file_management WHERE stage = 'pending' AND downloaded_at < ?",
            (cutoff,),
        )
