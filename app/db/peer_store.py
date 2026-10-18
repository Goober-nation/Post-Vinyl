"""
PeerStore — peers with a current bad record (migration 015, #67).

No row means a clean peer. A row exists while a peer has failures since its
last unblock; `blocked_at` is set once those reach
`download.bad_peer_threshold`. Unblocking — by expiry after
`download.peer_ban_days`, checked lazily on lookup, or by the "Unblock all
peers" button — deletes the row.

Failures caused by our own connection (timed out / aborted / cancelled) are
filtered out by the caller and never recorded here.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)


class PeerStore:
    """SQLite-backed store for the peers table."""

    def __init__(self, database):
        self._db = database

    def record_failure(self, username: str, threshold: int) -> tuple[int, bool]:
        """Count a failure against a peer.

        Returns (failure_count, newly_blocked): newly_blocked is True only on
        the failure that reaches `threshold`.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO peers (username, failure_count) VALUES (?, 1) "
                "ON CONFLICT(username) DO UPDATE SET failure_count = failure_count + 1",
                (username,),
            )
            row = conn.execute(
                "SELECT failure_count, blocked_at FROM peers WHERE username = ?",
                (username,),
            ).fetchone()
            newly_blocked = (
                row["blocked_at"] is None and row["failure_count"] >= threshold
            )
            if newly_blocked:
                conn.execute(
                    "UPDATE peers SET blocked_at = ? WHERE username = ?",
                    (now, username),
                )
        return row["failure_count"], newly_blocked

    def is_blocked(self, username: str, ban_seconds: int | None = None) -> bool:
        """Whether a peer is currently blocked.

        With `ban_seconds`, a block older than that is lifted on the spot:
        the row is deleted, so the peer starts clean.
        """
        row = self._db.fetch_one(
            "SELECT blocked_at FROM peers WHERE username = ?", (username,)
        )
        if row is None or row["blocked_at"] is None:
            return False
        if (
            ban_seconds is not None
            and int(time.time()) - row["blocked_at"] >= ban_seconds
        ):
            self._db.execute("DELETE FROM peers WHERE username = ?", (username,))
            logger.info("Peer %s unblocked: ban expired", username)
            return False
        return True

    def failure_count(self, username: str) -> int:
        row = self._db.fetch_one(
            "SELECT failure_count FROM peers WHERE username = ?", (username,)
        )
        return row["failure_count"] if row else 0

    def unblock_all(self) -> int:
        """Delete every blocked peer's row. Returns how many were unblocked."""
        cursor = self._db.execute("DELETE FROM peers WHERE blocked_at IS NOT NULL")
        return cursor.rowcount
