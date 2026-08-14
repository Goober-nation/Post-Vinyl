"""
SyncStore — SQLite persistence for the love/hate sync workers.

Backs the `sync_state` table (migration 001): one row per song, recording
whether its Navidrome star (love, +1) or Trash membership (hate, -1) has
been pushed to ListenBrainz. `lb_synced` is the retry flag: rows land with
lb_synced=0 when the feedback call failed or ListenBrainz was disabled, and
the worker re-attempts them on the next cycle.

Used by LoveSync (P6.7-5) and TrashPurge (P6.7-6).
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)

LOVE = "love"
HATE = "hate"


class SyncStore:
    """SQLite-backed store for sync_state rows."""

    def __init__(self, database):
        """
        Initialize SyncStore.

        Args:
            database: Database instance
        """
        self._db = database

    def record(
        self,
        song_id: str,
        song_type: str,
        mbid: str | None = None,
        lb_synced: int | bool = 0,
    ) -> None:
        """Insert or replace the sync_state row for a song.

        A song has one row no matter how its Navidrome intent changes over
        time (starred -> loved, trashed -> hated): the latest record wins.
        """
        self._db.execute(
            "INSERT OR REPLACE INTO sync_state "
            "(song_id, song_type, mbid, synced_at, lb_synced) "
            "VALUES (?, ?, ?, ?, ?)",
            (song_id, song_type, mbid, int(time.time()), int(lb_synced)),
        )

    def get(self, song_id: str) -> dict | None:
        """Return the sync_state row for a song, or None."""
        return self._db.fetch_one(
            "SELECT * FROM sync_state WHERE song_id = ?", (song_id,)
        )

    def needs_feedback(self, song_id: str, song_type: str) -> bool:
        """True when ListenBrainz feedback for this song+intent is outstanding.

        Outstanding means: no row at all, a row whose lb_synced is still 0
        (a previous attempt failed or was skipped because ListenBrainz was
        disabled), or a row recording a *different* intent — a love row must
        not suppress a later hate and vice versa.
        """
        row = self._db.fetch_one(
            "SELECT song_type, lb_synced FROM sync_state WHERE song_id = ?",
            (song_id,),
        )
        if row is None:
            return True
        if row["song_type"] != song_type:
            return True
        return bool(not row["lb_synced"])

    def mark_feedback_synced(self, song_id: str) -> None:
        """Mark a song's feedback as delivered; clears it for re-sync."""
        self._db.execute(
            "UPDATE sync_state SET lb_synced = 1, synced_at = ? WHERE song_id = ?",
            (int(time.time()), song_id),
        )
