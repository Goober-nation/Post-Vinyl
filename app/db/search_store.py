"""
SearchStore — SQLite persistence for user search headers.

Only user-initiated searches (from the search routes) are persisted here —
rec-puller's background searches are not, so the table only ever holds rows
a person would recognize from their own search history.

**Peer responses are never stored.** slskd is the system of record for
search results and retains them durably — verified live 2026-08-11: the
oldest search musica knew about still returned all 250 responses, and they
survived a `docker compose restart slskd` unchanged. Responses are re-fetched
from slskd by search_id when a saved search is reopened or a download needs
an alternative peer; see migration 005 for why the duplicate copy that
briefly lived here (004) was dropped.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)


class SearchStore:
    """SQLite-backed store for search headers (query/id, not peer responses)."""

    def __init__(self, database):
        """
        Initialize SearchStore.

        Args:
            database: Database instance
        """
        self._db = database

    def insert_search(
        self,
        search_id: str,
        query: str,
        artist: str | None,
        status: str,
    ) -> None:
        """Insert a search header row."""
        now = int(time.time())
        self._db.execute(
            "INSERT INTO searches (id, query, artist, created_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (search_id, query, artist, now, status),
        )

    def update_status(
        self,
        search_id: str,
        status: str,
        response_count: int | None = None,
        file_count: int | None = None,
    ) -> None:
        """Update a search's status and optionally its result counts.

        A no-op for searches with no header row — rec-puller's background
        searches never get one, and SlskdSearch calls this on every status
        change regardless of origin.
        """
        self._db.execute(
            "UPDATE searches SET "
            "status = ?, "
            "response_count = COALESCE(?, response_count), "
            "file_count = COALESCE(?, file_count) "
            "WHERE id = ?",
            (status, response_count, file_count, search_id),
        )

    def get_search(self, search_id: str) -> dict | None:
        """Get a single search header by id."""
        return self._db.fetch_one("SELECT * FROM searches WHERE id = ?", (search_id,))

    def all_searches(self) -> list[dict]:
        """Every persisted search header, for rebuilding the in-memory job
        map on startup.

        Headers only and user-initiated only, so this stays small — unlike
        the response table it replaces, which grew ~113 rows per search.
        """
        return self._db.fetch_all("SELECT * FROM searches")

    def list_recent(self, limit: int = 20) -> list[dict]:
        """List the most recent search headers, newest first, capped at `limit`."""
        return self._db.fetch_all(
            "SELECT * FROM searches ORDER BY created_at DESC LIMIT ?", (limit,)
        )
