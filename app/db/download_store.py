"""
DownloadStore — SQLite persistence for download and peer state.

Replaces the in-memory dicts in SlskdDownload with durable storage.
Used by the DownloadMonitor worker and the download routes.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)

# Audio extensions for peer file filtering
ALLOWED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac"}


class DownloadStore:
    """SQLite-backed store for download transfers and peer reputation."""

    def __init__(self, database):
        """
        Initialize DownloadStore.

        Args:
            database: Database instance
        """
        self._db = database

    # ------------------------------------------------------------------
    # Pending rows (queue-time, before slskd assigns a UUID)
    # ------------------------------------------------------------------

    def insert_pending(
        self,
        search_id: str,
        username: str,
        filename: str,
        size: int,
        is_rec_download: bool,
        is_library_download: bool = False,
        mb_recording_id: str | None = None,
        retry_count: int = 0,
    ) -> str:
        """Insert a pending download row before slskd assigns a transfer UUID.

        `is_library_download` / `mb_recording_id` (P6.8) mark a MusicBrainz-
        initiated download: it imports into the beets "library" profile,
        pinned to `mb_recording_id`. Defaults keep every existing caller on
        the plain manual path.
        """
        download_id = f"pending:{username}:{filename}:{int(time.time())}"
        now = int(time.time())
        self._db.execute(
            "INSERT INTO downloads (id, search_id, username, filename, size, "
            "state, retry_count, is_rec_download, created_at, target_dir, "
            "slskd_id, progress, speed, file_moved, mb_recording_id, "
            "is_library_download) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, 0, NULL, 0, ?, ?)",
            (
                download_id,
                search_id,
                username,
                filename,
                size,
                retry_count,
                int(is_rec_download),
                now,
                mb_recording_id,
                int(is_library_download),
            ),
        )
        logger.debug(f"Inserted pending download: {download_id}")
        return download_id

    def get_pending_search_id(self, username: str, filename: str) -> str | None:
        """Get the search_id stored on a download row (pending or adopted)."""
        row = self._db.fetch_one(
            "SELECT search_id FROM downloads "
            "WHERE username = ? AND filename = ? AND search_id IS NOT NULL "
            "LIMIT 1",
            (username, filename),
        )
        return row["search_id"] if row else None

    # ------------------------------------------------------------------
    # Transfer upsert (worker-driven from slskd state)
    # ------------------------------------------------------------------

    def upsert_transfer(self, transfer) -> tuple[bool, str | None]:
        """
        Insert or update a download row from a Transfer object.

        Returns (is_new: bool, prev_state: str | None).
        """
        tid = transfer.transfer_id
        username = transfer.username
        filename = transfer.filename

        # (a) Match by slskd_id first
        existing = self._db.fetch_one(
            "SELECT id, state FROM downloads WHERE slskd_id = ?", (tid,)
        )
        if existing:
            prev_state = existing["state"]
            self._db.execute(
                "UPDATE downloads SET state = ?, progress = ?, speed = ?, "
                "completed_at = ? WHERE slskd_id = ?",
                (
                    transfer.state,
                    transfer.progress,
                    transfer.speed,
                    int(time.time())
                    if transfer.state in ("completed", "failed", "cancelled")
                    else None,
                    tid,
                ),
            )
            return (False, prev_state)

        # (b) Match pending row by username+filename (queue-time row)
        pending = self._db.fetch_one(
            "SELECT id, state, search_id, is_rec_download, created_at FROM downloads "
            "WHERE username = ? AND filename = ? "
            "AND (slskd_id IS NULL OR id LIKE 'pending:%') "
            "LIMIT 1",
            (username, filename),
        )
        if pending:
            prev_state = pending["state"]
            # Adopt: rewrite the id to the slskd UUID
            now = int(time.time())
            self._db.execute(
                "UPDATE downloads SET id = ?, slskd_id = ?, state = ?, "
                "progress = ?, speed = ?, completed_at = ? "
                "WHERE id = ?",
                (
                    tid,
                    tid,
                    transfer.state,
                    transfer.progress,
                    transfer.speed,
                    now
                    if transfer.state in ("completed", "failed", "cancelled")
                    else None,
                    pending["id"],
                ),
            )
            return (False, prev_state)

        # (c) New row
        now = int(time.time())
        self._db.execute(
            "INSERT INTO downloads (id, slskd_id, search_id, username, filename, "
            "size, state, retry_count, is_rec_download, created_at, completed_at, "
            "target_dir, progress, speed, file_moved) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, 0, ?, ?, ?, NULL, ?, ?, 0)",
            (
                tid,
                tid,
                username,
                filename,
                transfer.size,
                transfer.state,
                int(transfer.is_rec_download),
                now,
                now if transfer.state in ("completed", "failed", "cancelled") else None,
                transfer.progress,
                transfer.speed,
            ),
        )
        return (True, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_transfer(self, transfer_id: str) -> dict | None:
        """Get a download row by transfer id."""
        return self._db.fetch_one(
            "SELECT * FROM downloads WHERE id = ?", (transfer_id,)
        )

    def get_transfers_by_state(self, state: str) -> list[dict]:
        """Get all downloads in a given state."""
        return self._db.fetch_all("SELECT * FROM downloads WHERE state = ?", (state,))

    def get_adopted_live_transfers(self) -> list[dict]:
        """Rows slskd has adopted that are still in flight.

        Only rows with a real slskd id — queue-time 'pending:' rows are
        excluded because slskd has never confirmed them, so their absence
        from a status poll means nothing. Drives orphan reconciliation:
        these are the rows slskd *should* still be reporting.
        """
        return self._db.fetch_all(
            "SELECT id, username, filename, is_rec_download FROM downloads "
            "WHERE state IN ('queued', 'downloading') AND slskd_id IS NOT NULL"
        )

    def get_stranded_downloads(self) -> list[dict]:
        """Rows whose import was declined and whose file was never moved
        (P6.7-6 stranded sweep).

        `import_skipped` is terminal for DownloadMonitor — a duplicate beets
        refused, or a 'completed' transfer whose source file never showed up
        on disk within missing_source_timeout_minutes. The file (when it
        exists at all) sits in `downloads/complete/soulseek/<username>/`
        with nothing else disposing of it, so TrashPurge removes it and
        prunes the empty directories it left behind.
        """
        return self._db.fetch_all(
            "SELECT id, username, filename FROM downloads "
            "WHERE import_skipped = 1 AND file_moved = 0"
        )

    def mark_failed(self, transfer_ids: list[str]) -> int:
        """Move download rows to 'failed'. Returns rowcount."""
        if not transfer_ids:
            return 0
        placeholders = ",".join("?" for _ in transfer_ids)
        cursor = self._db.execute(
            f"UPDATE downloads SET state = 'failed', completed_at = ? "
            f"WHERE id IN ({placeholders})",
            (int(time.time()), *transfer_ids),
        )
        return cursor.rowcount

    def get_downloads_by_search_ids(self, search_ids: list[str]) -> list[dict]:
        """Get all download rows whose search_id is in the given list."""
        if not search_ids:
            return []
        placeholders = ",".join("?" for _ in search_ids)
        return self._db.fetch_all(
            f"SELECT * FROM downloads WHERE search_id IN ({placeholders})",
            tuple(search_ids),
        )

    def has_active_manual_downloads(
        self, queued_grace_seconds: int | None = None
    ) -> bool:
        """True while any manual (non-rec) download is queued or in progress.

        Drives RecPuller's queue-priority gate (P6.5-5): rec queueing pauses
        while a manual transfer is in flight, so manual downloads always
        beat recommendations. 'queued' includes queue-time pending rows
        (adopted by DownloadMonitor once slskd reports the transfer).

        Library downloads (P6.8) are manual here: they carry
        `is_rec_download = 0`, so the `is_rec_download = 0` filter already
        treats them as gate-holding manual work with no change needed.

        `queued_grace_seconds` caps how long a *still-queued* row may hold
        the gate. Found live 2026-08-11: a manual download adopted by slskd
        and left in "Queued, Remotely" — the peer has it in *their* upload
        queue — held the gate for 11+ minutes and would have held it for
        hours. That's routine Soulseek behavior, not an error, so nothing
        else would ever clear it: it isn't an orphan (slskd keeps reporting
        it) and it isn't a stale pending row (it has a real slskd id). Recs
        would simply stop.

        Rows in 'downloading' are never aged out — bytes are moving, and a
        large file legitimately takes a long time. Only the "waiting on a
        peer that may never get to us" case is time-limited, and even then
        the transfer itself is untouched: it keeps going and still completes
        if the peer comes through. It just stops starving recs.

        A pending row slskd never adopts is handled separately by
        fail_stale_pending(), which moves it out of 'queued' entirely.
        """
        if queued_grace_seconds is None:
            row = self._db.fetch_one(
                "SELECT 1 FROM downloads "
                "WHERE state IN ('queued', 'downloading') AND is_rec_download = 0 "
                "LIMIT 1"
            )
            return row is not None

        cutoff = int(time.time()) - queued_grace_seconds
        row = self._db.fetch_one(
            "SELECT 1 FROM downloads WHERE is_rec_download = 0 AND ("
            "  state = 'downloading'"
            "  OR (state = 'queued' AND created_at >= ?)"
            ") LIMIT 1",
            (cutoff,),
        )
        return row is not None

    def fail_stale_pending(self, older_than_seconds: int) -> list[dict]:
        """Mark queue-time pending rows slskd never adopted as failed.

        A row inserted by insert_pending() keeps its 'pending:' id and a
        NULL slskd_id until DownloadMonitor sees the matching transfer.
        If slskd never reports it, the row sits in 'queued' forever: it
        blocks rec queueing via has_active_manual_downloads(), it shows in
        the UI as a phantom active download, and no route can remove it
        (cancel needs a real slskd transfer id; the bulk delete only covers
        finished states).

        Moving it to 'failed' fixes all three at once — the gate only
        counts queued/downloading, the UI shows a failure with a reason,
        and 'failed' is in the routes' FINISHED_STATES so the existing
        delete-finished endpoint clears it. The row keeps its 'pending:'
        id and NULL slskd_id, so a late adoption still matches it in
        upsert_transfer() and overwrites the state with slskd's real one.

        Returns the rows that were failed (id, username, filename).
        """
        cutoff = int(time.time()) - older_than_seconds
        stale = self._db.fetch_all(
            "SELECT id, username, filename FROM downloads "
            "WHERE id LIKE 'pending:%' AND slskd_id IS NULL "
            "AND state = 'queued' AND created_at < ?",
            (cutoff,),
        )
        if not stale:
            return []
        placeholders = ",".join("?" for _ in stale)
        self._db.execute(
            f"UPDATE downloads SET state = 'failed', completed_at = ? "
            f"WHERE id IN ({placeholders})",
            (int(time.time()), *(row["id"] for row in stale)),
        )
        return stale

    def delete_transfers(self, transfer_ids: list[str]) -> int:
        """Permanently delete download rows by id. Returns rowcount."""
        if not transfer_ids:
            return 0
        placeholders = ",".join("?" for _ in transfer_ids)
        cursor = self._db.execute(
            f"DELETE FROM downloads WHERE id IN ({placeholders})",
            tuple(transfer_ids),
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Retry tracking
    # ------------------------------------------------------------------

    def get_retry_count(self, transfer_id: str) -> int:
        """Get retry_count for a transfer."""
        row = self._db.fetch_one(
            "SELECT retry_count FROM downloads WHERE id = ?", (transfer_id,)
        )
        return row["retry_count"] if row else 0

    def increment_retry_count(self, transfer_id: str) -> None:
        """Increment retry_count for a transfer."""
        self._db.execute(
            "UPDATE downloads SET retry_count = retry_count + 1 WHERE id = ?",
            (transfer_id,),
        )

    def record_retry_attempt(
        self,
        transfer_id: str,
        username: str,
        success: bool,
    ) -> None:
        """Record a retry attempt in download_retry_attempts."""
        self._db.execute(
            "INSERT INTO download_retry_attempts "
            "(download_id, username, attempted_at, success) VALUES (?, ?, ?, ?)",
            (transfer_id, username, int(time.time()), int(success)),
        )

    # ------------------------------------------------------------------
    # Peer reputation
    # ------------------------------------------------------------------

    def increment_peer_failure(self, username: str) -> int:
        """Increment a peer's failure count. Returns the new count."""
        now = int(time.time())
        existing = self._db.fetch_one(
            "SELECT failure_count FROM peers WHERE username = ?", (username,)
        )
        if existing:
            new_count = existing["failure_count"] + 1
            self._db.execute(
                "UPDATE peers SET failure_count = ?, last_seen = ? WHERE username = ?",
                (new_count, now, username),
            )
        else:
            new_count = 1
            self._db.execute(
                "INSERT INTO peers (username, failure_count, last_seen) "
                "VALUES (?, ?, ?)",
                (username, new_count, now),
            )
        return new_count

    def set_peer_blocked(self, username: str) -> None:
        """Mark a peer as blocked."""
        self._db.execute(
            "UPDATE peers SET is_blocked = 1, blocked_at = ? WHERE username = ?",
            (int(time.time()), username),
        )

    def is_peer_blocked(
        self, username: str, ban_duration_seconds: int | None = None
    ) -> bool:
        """Check if a peer is blocked.

        `ban_duration_seconds`, when given, makes a block temporary: a peer
        blocked longer ago than this is unbanned on the spot — `is_blocked`
        cleared, `failure_count` reset to 0, so it gets a genuinely clean
        slate rather than starting one failure away from re-blocking.
        Checked lazily here rather than by a periodic sweep, so it can never
        drift out of sync with what a caller actually observes. Omitting it
        preserves the old permanent-ban behavior (existing callers/tests).
        """
        row = self._db.fetch_one(
            "SELECT is_blocked, blocked_at FROM peers WHERE username = ?", (username,)
        )
        if not row or not row["is_blocked"]:
            return False
        if ban_duration_seconds is None:
            return True
        blocked_at = row["blocked_at"]
        if (
            blocked_at is not None
            and int(time.time()) - blocked_at >= ban_duration_seconds
        ):
            self._db.execute(
                "UPDATE peers SET is_blocked = 0, blocked_at = NULL, failure_count = 0 "
                "WHERE username = ?",
                (username,),
            )
            return False
        return True

    def unblock_all_peers(self) -> int:
        """Clear every peer block and reset failure counts. Manual escape
        hatch for a burst ban caused by something other than actual peer
        misbehavior (e.g. before the connectivity-vs-peer-fault distinction
        in DownloadMonitor existed, a wifi/VPN reconnect could ban every
        currently-transferring peer at once for `peer_ban_days`). Returns
        the number of peers that were actually blocked.
        """
        row = self._db.fetch_one("SELECT COUNT(*) AS n FROM peers WHERE is_blocked = 1")
        count = row["n"] if row else 0
        self._db.execute(
            "UPDATE peers SET is_blocked = 0, blocked_at = NULL, failure_count = 0 "
            "WHERE is_blocked = 1"
        )
        return count

    def get_peer_failure_count(self, username: str) -> int:
        """Get a peer's failure count (0 if unknown)."""
        row = self._db.fetch_one(
            "SELECT failure_count FROM peers WHERE username = ?", (username,)
        )
        return row["failure_count"] if row else 0

    # ------------------------------------------------------------------
    # File move tracking
    # ------------------------------------------------------------------

    def mark_file_moved(self, transfer_id: str, target_dir: str) -> None:
        """Mark a transfer's file as moved to target_dir.

        `target_dir` is required: file_moved = 1 is what the UI, the
        transfer.completed SSE payload and /api/downloads all read as "the
        file is at target_dir", so recording a move with no destination
        makes the file unfindable. A caller that did not move anything wants
        mark_import_skipped() instead (migration 007).
        """
        if not target_dir:
            raise ValueError(
                "mark_file_moved requires a target_dir; use "
                "mark_import_skipped() for a file that was not moved"
            )
        self._db.execute(
            "UPDATE downloads SET file_moved = 1, target_dir = ? WHERE id = ?",
            (target_dir, transfer_id),
        )

    def mark_import_skipped(self, transfer_id: str) -> None:
        """Record that the import was declined and the file was NOT moved.

        Terminal for the monitor's purposes — re-running the import would
        hit the same refusal every poll — without claiming a move that
        never happened. target_dir is deliberately left alone.
        """
        self._db.execute(
            "UPDATE downloads SET import_skipped = 1 WHERE id = ?",
            (transfer_id,),
        )

    def file_moved(self, transfer_id: str) -> bool:
        """Check if a transfer's file has been moved."""
        row = self._db.fetch_one(
            "SELECT file_moved FROM downloads WHERE id = ?", (transfer_id,)
        )
        return bool(row["file_moved"]) if row else False

    def import_handled(self, transfer_id: str) -> bool:
        """Is there anything left to do about this download's file?

        True once it has been moved into a library tree *or* beets has
        declined it. This is the monitor's "don't run the import again"
        gate — file_moved() alone is not, since a skipped import is equally
        terminal but moved nothing.
        """
        row = self._db.fetch_one(
            "SELECT file_moved, import_skipped FROM downloads WHERE id = ?",
            (transfer_id,),
        )
        if not row:
            return False
        return bool(row["file_moved"]) or bool(row["import_skipped"])

    def import_pending(self, transfer_id: str) -> bool:
        """Does musica have a download row for this transfer that beets
        still has work left to do on?

        Not simply `not import_handled()`: that would also be True when
        there is no row at all for this transfer_id, which is a different
        thing entirely — it means musica has no record of this transfer
        (e.g. slskd reporting a transfer from before musica's last reset,
        which keeps its own history independently). "No memory of it" is
        not "still importing"; callers surfacing progress to a user (the
        `/api/transfers` "importing" label, the delete-finished eligibility
        check) want False for both "already done" and "not ours" alike.
        """
        row = self._db.fetch_one(
            "SELECT file_moved, import_skipped FROM downloads WHERE id = ?",
            (transfer_id,),
        )
        if not row:
            return False
        return not (bool(row["file_moved"]) or bool(row["import_skipped"]))

    def set_import_unmatched(self, transfer_id: str, unmatched: bool) -> None:
        """Flag whether a beets import matched MusicBrainz metadata (P6.6-4)."""
        self._db.execute(
            "UPDATE downloads SET import_unmatched = ? WHERE id = ?",
            (int(unmatched), transfer_id),
        )

    # ------------------------------------------------------------------
    # Recommendation completion hook
    # ------------------------------------------------------------------

    def mark_rec_downloaded(self, search_id: str, download_id: str) -> int:
        """Mark queued recommendation rows as downloaded (link via search_id). Returns rowcount."""
        now = int(time.time())
        cursor = self._db.execute(
            "UPDATE recommendations SET status = 'downloaded', "
            "download_id = ?, processed_at = ? "
            "WHERE search_id = ? AND status = 'queued'",
            (download_id, now, search_id),
        )
        return cursor.rowcount
