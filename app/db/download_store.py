"""
DownloadStore — the network side of a download (migration 015, #67).

One `downloads` row per requested file, kept through every retry: a retry
moves the same row to a new peer instead of inserting a new one. Every peer
ever tried is a `download_attempts` row (the first is the original request),
so peer selection can skip all of them and the retry count is just
attempts - 1.

The row is always written *before* slskd is asked to queue the file, with
`slskd_id` still NULL. DownloadMonitor fills `slskd_id` in when slskd first
reports the transfer (adopt()). slskd transfers with no matching row were
not started by this app and are ignored.

When a row reaches 'completed', the `downloads_completed_handoff` trigger
creates its file_management row; after that the download row and its slskd
record can both be deleted (handed_off()).

slskd owns live transfer state (progress, speed); none of it is stored here.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)

# Audio extensions for peer file filtering
ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}

TERMINAL_STATES = ("completed", "failed", "cancelled")
ACTIVE_STATES = ("queued", "downloading")


class DownloadStore:
    """SQLite-backed store for downloads and their attempts."""

    def __init__(self, database):
        self._db = database

    # ------------------------------------------------------------------
    # Queueing and retry
    # ------------------------------------------------------------------

    def insert_queued(
        self,
        request_id: int,
        search_id: str,
        username: str,
        filename: str,
        size: int | None = None,
    ) -> int:
        """Write a download row and its first attempt; return the row id.

        Call this before asking slskd to queue the file, then
        mark_attempt_accepted() once slskd accepts it.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            download_id = conn.execute(
                "INSERT INTO downloads (request_id, search_id, username, filename, size, "
                "state, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (request_id, search_id, username, filename, size, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO download_attempts (download_id, username, attempted_at) "
                "VALUES (?, ?, ?)",
                (download_id, username, now),
            )
        return download_id

    def insert_pending(
        self,
        search_id: str,
        username: str,
        filename: str,
        size: int | None = None,
        is_rec: bool = False,
        mb_recording_id: str | None = None,
        source: str | None = None,
    ) -> int:
        """Create the request, search and download rows for one queued file.

        Writes, in one transaction:
          - a `requests` row (source 'rec' or 'slskd' unless `source` says
            otherwise, e.g. 'catalog', with the MusicBrainz recording id when
            given);
          - a `slskd_searches` row for `search_id`, only if it does not exist
            yet. An existing row keeps its request_id: a search may serve
            several requests, and the first one owns it;
          - the `downloads` row in state 'queued';
          - its first `download_attempts` row (success = 0), since the
            original request is the first peer tried.

        Returns the new downloads.id. Call mark_attempt_accepted() once slskd
        accepts the queue request.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            request_id = conn.execute(
                "INSERT INTO requests (source, mbid, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?)",
                (source or ("rec" if is_rec else "slskd"), mb_recording_id, now, now),
            ).lastrowid
            exists = conn.execute(
                "SELECT 1 FROM slskd_searches WHERE id = ?", (search_id,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO slskd_searches (id, request_id, query, created_at) "
                    "VALUES (?, ?, '', ?)",
                    (search_id, request_id, now),
                )
            download_id = conn.execute(
                "INSERT INTO downloads (request_id, search_id, username, filename, size, "
                "state, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (request_id, search_id, username, filename, size, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO download_attempts (download_id, username, attempted_at, success) "
                "VALUES (?, ?, ?, 0)",
                (download_id, username, now),
            )
        return download_id

    def import_pending(self, username: str, filename: str) -> bool:
        """True when a file_management row for this peer file is at stage 'pending'.

        A pure lookup: it changes nothing. The caller decides the next stage
        (FileStore.mark_moved / mark_missing).
        """
        row = self._db.fetch_one(
            "SELECT 1 FROM file_management WHERE username = ? AND filename = ? "
            "AND stage = 'pending' LIMIT 1",
            (username, filename),
        )
        return row is not None

    def move_to_peer(
        self, download_id: int, username: str, filename: str, size: int | None = None
    ) -> None:
        """Point an existing download at a new peer (a retry).

        Resets it to 'queued' with no slskd id and records the attempt. Call
        before asking slskd to queue the new peer's file.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE downloads SET username = ?, filename = ?, size = ?, slskd_id = NULL, "
                "state = 'queued', completed_at = NULL WHERE id = ?",
                (username, filename, size, download_id),
            )
            conn.execute(
                "INSERT INTO download_attempts (download_id, username, attempted_at) "
                "VALUES (?, ?, ?)",
                (download_id, username, now),
            )

    def mark_attempt_accepted(self, download_id: int) -> None:
        """Mark the download's latest attempt as accepted by slskd."""
        self._db.execute(
            "UPDATE download_attempts SET success = 1 WHERE id = ("
            "  SELECT id FROM download_attempts WHERE download_id = ? "
            "  ORDER BY id DESC LIMIT 1)",
            (download_id,),
        )

    def tried_peers(self, download_id: int) -> set[str]:
        """Every peer this download has been queued from, current one included."""
        rows = self._db.fetch_all(
            "SELECT DISTINCT username FROM download_attempts WHERE download_id = ?",
            (download_id,),
        )
        return {row["username"] for row in rows}

    def latest_attempt_at(self, download_id: int) -> int | None:
        """When this download was last queued to a peer (its current attempt)."""
        row = self._db.fetch_one(
            "SELECT MAX(attempted_at) AS at FROM download_attempts WHERE download_id = ?",
            (download_id,),
        )
        return row["at"] if row and row["at"] is not None else None

    def retry_count(self, download_id: int) -> int:
        """Retries so far: attempts minus the original request."""
        row = self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM download_attempts WHERE download_id = ?",
            (download_id,),
        )
        return max(0, (row["n"] if row else 0) - 1)

    # ------------------------------------------------------------------
    # Tracking slskd's transfers
    # ------------------------------------------------------------------

    def adopt(self, username: str, filename: str, slskd_id: str) -> dict | None:
        """Link a transfer slskd just reported to the row queued for it.

        Matches the most recent row for that peer and file that slskd hasn't
        reported yet. Returns the updated row, or None when no row matches —
        a transfer this app didn't start, which callers ignore.
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM downloads WHERE username = ? AND filename = ? "
                "AND slskd_id IS NULL AND state = 'queued' ORDER BY id DESC LIMIT 1",
                (username, filename),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE downloads SET slskd_id = ? WHERE id = ?", (slskd_id, row["id"])
            )
            adopted = conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (row["id"],)
            ).fetchone()
        return dict(adopted)

    def set_state(self, download_id: int, state: str) -> str | None:
        """Update a download's state; returns the previous state.

        Terminal states stamp completed_at. Reaching 'completed' fires the
        file_management handoff trigger.
        """
        row = self._db.fetch_one(
            "SELECT state FROM downloads WHERE id = ?", (download_id,)
        )
        if row is None:
            return None
        completed_at = int(time.time()) if state in TERMINAL_STATES else None
        self._db.execute(
            "UPDATE downloads SET state = ?, completed_at = ? WHERE id = ?",
            (state, completed_at, download_id),
        )
        return row["state"]

    def get(self, download_id: int) -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM downloads WHERE id = ?", (download_id,)
        )

    def get_by_slskd_id(self, slskd_id: str) -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM downloads WHERE slskd_id = ?", (slskd_id,)
        )

    def get_transfers_by_state(self, state: str) -> list[dict]:
        return self._db.fetch_all(
            "SELECT * FROM downloads WHERE state = ? ORDER BY id", (state,)
        )

    def get_pending_search_id(self, username: str, filename: str) -> str | None:
        """The search a file was queued from, for the newest row that has it.

        Replaces the old pending-row lookup. Retry re-reads that search's
        responses from slskd rather than starting a new one. None when no row
        exists for this peer file.
        """
        row = self._db.fetch_one(
            "SELECT search_id FROM downloads WHERE username = ? AND filename = ? "
            "ORDER BY id DESC LIMIT 1",
            (username, filename),
        )
        return row["search_id"] if row else None

    def get_with_request(self, download_id: int) -> dict | None:
        """A download plus its request's source and mbid."""
        return self._db.fetch_one(
            "SELECT d.*, r.source, r.mbid FROM downloads d "
            "JOIN requests r ON r.id = d.request_id WHERE d.id = ?",
            (download_id,),
        )

    def adopted_active(self) -> list[dict]:
        """In-flight rows slskd has reported, i.e. ones it should keep
        reporting. Drives orphan reconciliation."""
        return self._db.fetch_all(
            "SELECT * FROM downloads WHERE state IN ('queued', 'downloading') "
            "AND slskd_id IS NOT NULL"
        )

    def fail_stale_unadopted(self, older_than_seconds: int) -> list[dict]:
        """Fail rows slskd never reported within `older_than_seconds` of
        their latest attempt. Returns the rows that were failed."""
        cutoff = int(time.time()) - older_than_seconds
        stale = self._db.fetch_all(
            "SELECT d.* FROM downloads d WHERE d.state = 'queued' AND d.slskd_id IS NULL "
            "AND (SELECT MAX(attempted_at) FROM download_attempts a "
            "     WHERE a.download_id = d.id) < ?",
            (cutoff,),
        )
        for row in stale:
            self.set_state(row["id"], "failed")
        return stale

    def has_active_manual_downloads(
        self, queued_grace_seconds: int | None = None
    ) -> bool:
        """True while a non-rec download is queued or in progress.

        A still-queued row only counts for `queued_grace_seconds` after its
        latest attempt: a peer can hold a file in its own upload queue for
        hours, which is routine on Soulseek and must not block recs forever.
        Rows in 'downloading' always count.
        """
        if queued_grace_seconds is None:
            row = self._db.fetch_one(
                "SELECT 1 FROM downloads d JOIN requests r ON r.id = d.request_id "
                "WHERE d.state IN ('queued', 'downloading') AND r.source <> 'rec' LIMIT 1"
            )
            return row is not None
        cutoff = int(time.time()) - queued_grace_seconds
        row = self._db.fetch_one(
            "SELECT 1 FROM downloads d JOIN requests r ON r.id = d.request_id "
            "WHERE r.source <> 'rec' AND (d.state = 'downloading' OR (d.state = 'queued' AND "
            "(SELECT MAX(attempted_at) FROM download_attempts a WHERE a.download_id = d.id) >= ?"
            ")) LIMIT 1",
            (cutoff,),
        )
        return row is not None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def handed_off(self) -> list[dict]:
        """Completed downloads whose file is no longer pending placement.

        The file_management row for a completed download is created by the
        handoff trigger and stays 'pending' until the file is moved, found
        missing, or unindexed. Once it has left 'pending' the download row
        and slskd's record can both go. While it is still pending the
        monitor still needs slskd to report the transfer so it can move the
        file, so those rows are not returned.
        """
        return self._db.fetch_all(
            "SELECT d.* FROM downloads d WHERE d.state = 'completed' AND NOT EXISTS ("
            "  SELECT 1 FROM file_management f "
            "  WHERE f.username = d.username AND f.filename = d.filename "
            "  AND f.stage = 'pending')"
        )

    def delete(self, download_ids: list[int]) -> int:
        """Delete download rows (their attempts cascade). Returns rowcount."""
        if not download_ids:
            return 0
        placeholders = ",".join("?" for _ in download_ids)
        cursor = self._db.execute(
            f"DELETE FROM downloads WHERE id IN ({placeholders})", tuple(download_ids)
        )
        return cursor.rowcount
