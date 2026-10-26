"""
RequestStore — origins, requests and slskd searches (migration 015, #67).

A request is one "get me this track", whatever started it. It optionally
points at the origin that caused it (a typed catalog search, later a rec),
and owns every slskd search run for it. Downloads hang off both.

slskd stays the system of record for search *results* and live search
state; only which searches exist and what they were for is stored here.

Deleting a request deletes its slskd searches, downloads and download
attempts through ON DELETE CASCADE; placed-file records in file_management
are independent and survive. Requests with a download still in flight are
never deleted (see _NO_ACTIVE_DOWNLOAD).
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)

#: A request may only be deleted when none of its downloads is in flight —
#: otherwise slskd keeps transferring with no row behind it.
_NO_ACTIVE_DOWNLOAD = (
    "NOT EXISTS (SELECT 1 FROM downloads d WHERE d.request_id = requests.id "
    "AND d.state IN ('queued', 'downloading'))"
)


class RequestStore:
    """SQLite-backed store for origins, requests and slskd searches."""

    def __init__(self, database):
        self._db = database

    # ------------------------------------------------------------------
    # Origins
    # ------------------------------------------------------------------

    def record_catalog_search(self, query: str, kind: str) -> int:
        """Record a typed catalog search and return its origin id.

        A repeat of the same query and kind (case-insensitive) reuses the
        existing row and bumps its last_used_at instead of adding another.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM catalog_searches WHERE lower(query) = lower(?) AND kind = ?",
                (query, kind),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE catalog_searches SET last_used_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return row["id"]
            origin_id = conn.execute(
                "INSERT INTO origins (kind) VALUES ('catalog_search')"
            ).lastrowid
            conn.execute(
                "INSERT INTO catalog_searches (id, query, kind, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (origin_id, query, kind, now, now),
            )
            return origin_id

    def list_catalog_searches(self, limit: int = 20) -> list[dict]:
        """Typed catalog searches, most recently used first."""
        return self._db.fetch_all(
            "SELECT * FROM catalog_searches ORDER BY last_used_at DESC LIMIT ?",
            (limit,),
        )

    def delete_origin(self, origin_id: int) -> bool:
        """Delete an origin of any kind.

        Always delete through `origins`: the type-specific row (e.g.
        catalog_searches) cascades with it, and requests that pointed at it
        keep everything but their origin_id.
        """
        cursor = self._db.execute("DELETE FROM origins WHERE id = ?", (origin_id,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def create_request(
        self,
        source: str,
        query: str | None = None,
        mbid: str | None = None,
        origin_id: int | None = None,
    ) -> int:
        """Insert a request and return its id."""
        now = int(time.time())
        cursor = self._db.execute(
            "INSERT INTO requests (source, origin_id, query, mbid, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, origin_id, query, mbid, now, now),
        )
        return cursor.lastrowid

    def get_or_create_slskd_request(self, query: str) -> int:
        """Request for a typed Soulseek-tab search.

        A repeat of the same query (case-insensitive) reuses the existing
        request and bumps its last_used_at, so the history shows it once.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM requests WHERE source = 'slskd' AND lower(query) = lower(?)",
                (query,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE requests SET last_used_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return row["id"]
            return conn.execute(
                "INSERT INTO requests (source, query, created_at, last_used_at) "
                "VALUES ('slskd', ?, ?, ?)",
                (query, now, now),
            ).lastrowid

    def get_request(self, request_id: int) -> dict | None:
        return self._db.fetch_one("SELECT * FROM requests WHERE id = ?", (request_id,))

    def list_recent_requests(
        self, source: str | None = None, limit: int = 20
    ) -> list[dict]:
        """Requests, most recently used first, optionally for one source."""
        if source is None:
            return self._db.fetch_all(
                "SELECT * FROM requests ORDER BY last_used_at DESC LIMIT ?", (limit,)
            )
        return self._db.fetch_all(
            "SELECT * FROM requests WHERE source = ? ORDER BY last_used_at DESC LIMIT ?",
            (source, limit),
        )

    def delete_request(self, request_id: int) -> bool:
        """Delete one request unless it has a download in flight.

        Returns False when nothing was deleted (unknown id, or still active).
        """
        cursor = self._db.execute(
            f"DELETE FROM requests WHERE id = ? AND {_NO_ACTIVE_DOWNLOAD}",
            (request_id,),
        )
        return cursor.rowcount > 0

    def clear_requests(self, source: str | None = None) -> int:
        """Delete every request (optionally of one source) with no download
        in flight. Returns how many were deleted."""
        if source is None:
            cursor = self._db.execute(
                f"DELETE FROM requests WHERE {_NO_ACTIVE_DOWNLOAD}"
            )
        else:
            cursor = self._db.execute(
                f"DELETE FROM requests WHERE source = ? AND {_NO_ACTIVE_DOWNLOAD}",
                (source,),
            )
        return cursor.rowcount

    def prune_requests(self, older_than_seconds: int) -> int:
        """Delete requests not used for `older_than_seconds`, skipping any
        with a download in flight. Returns how many were deleted."""
        cutoff = int(time.time()) - older_than_seconds
        cursor = self._db.execute(
            f"DELETE FROM requests WHERE last_used_at < ? AND {_NO_ACTIVE_DOWNLOAD}",
            (cutoff,),
        )
        if cursor.rowcount:
            logger.info(
                "Pruned %d request(s) older than %ds",
                cursor.rowcount,
                older_than_seconds,
            )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # slskd searches
    # ------------------------------------------------------------------

    def add_slskd_search(
        self, search_id: str, request_id: int, query: str, artist: str | None = None
    ) -> None:
        """Record a slskd search run for a request.

        `search_id` must be slskd's real id. The query cache only reuses a
        search within the request that ran it (each search has exactly one
        owner, so pruning a request can't cascade into another request's
        downloads); recording the same id again for that request is a no-op.

        Raises ValueError if the search already belongs to another request.
        """
        self._db.execute(
            "INSERT OR IGNORE INTO slskd_searches (id, request_id, query, artist, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (search_id, request_id, query, artist, int(time.time())),
        )
        owner = self._db.fetch_one(
            "SELECT request_id FROM slskd_searches WHERE id = ?", (search_id,)
        )
        if owner is not None and owner["request_id"] != request_id:
            raise ValueError(
                f"slskd search {search_id} belongs to request {owner['request_id']}, "
                f"not {request_id}; the query cache must not reuse it across requests"
            )

    def get_slskd_search(self, search_id: str) -> dict | None:
        return self._db.fetch_one(
            "SELECT * FROM slskd_searches WHERE id = ?", (search_id,)
        )

    def searches_for_request(self, request_id: int) -> list[dict]:
        """A request's slskd searches, oldest first (ladder order)."""
        return self._db.fetch_all(
            "SELECT * FROM slskd_searches WHERE request_id = ? ORDER BY created_at, rowid",
            (request_id,),
        )
