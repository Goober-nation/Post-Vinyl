"""
RecsStore — SQLite persistence for recommendation tracking.

Used by the RecPuller worker to track recommendation lifecycle:
fetch → classify → playlist-add / slskd-download.
"""

import time

from app.logging_config import get_logger

logger = get_logger(__name__)


class RecsStore:
    """SQLite-backed store for recommendation rows."""

    def __init__(self, database):
        """
        Initialize RecsStore.

        Args:
            database: Database instance
        """
        self._db = database

    def insert_rec(
        self,
        source: str,
        artist: str,
        track: str,
        mbid: str | None,
        status: str,
        search_id: str | None = None,
        playlist_id: str | None = None,
    ) -> int:
        """Insert a recommendation row. Returns the new row id."""
        now = int(time.time())
        cursor = self._db.execute(
            "INSERT INTO recommendations "
            "(source, artist, track, mbid, status, search_id, playlist_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, artist, track, mbid, status, search_id, playlist_id, now),
        )
        return cursor.lastrowid

    def update_status(
        self,
        rec_id: int,
        status: str,
        search_id: str | None = None,
        download_id: str | None = None,
        playlist_id: str | None = None,
    ) -> None:
        """Update recommendation status and optionally link search/download/playlist IDs."""
        now = int(time.time())
        self._db.execute(
            "UPDATE recommendations SET "
            "status = ?,"
            "search_id = COALESCE(?, search_id),"
            "download_id = COALESCE(?, download_id),"
            "playlist_id = COALESCE(?, playlist_id),"
            "processed_at = ? "
            "WHERE id = ?",
            (status, search_id, download_id, playlist_id, now, rec_id),
        )

    def get_recs_by_status(self, status: str) -> list[dict]:
        """Get all recommendations with a given status."""
        return self._db.fetch_all(
            "SELECT * FROM recommendations WHERE status = ?", (status,)
        )

    # Statuses that mean "already being handled, don't reprocess" — see
    # get_active_keys. Terminal-failure statuses (error, search_failed,
    # queue_failed) are deliberately excluded so those recs retry on the
    # next pull instead of being stuck forever (the actual fix behind the
    # cross-pull retry gap).
    ACTIVE_STATUSES = ("in_library", "queued", "downloaded")

    def get_active_recs(self) -> list[dict]:
        """Rows currently considered active (see ACTIVE_STATUSES).

        Used by RecPuller to skip re-fetching/re-classifying/re-adding a
        rec it's already handling, and by reconciliation to find rows that
        may have gone stale against the real library.
        """
        placeholders = ",".join("?" for _ in self.ACTIVE_STATUSES)
        return self._db.fetch_all(
            f"SELECT * FROM recommendations WHERE status IN ({placeholders})",
            self.ACTIVE_STATUSES,
        )

    def get_rec(self, rec_id: int) -> dict | None:
        """Get a single recommendation by id."""
        return self._db.fetch_one(
            "SELECT * FROM recommendations WHERE id = ?", (rec_id,)
        )

    def get_rec_by_search_id(self, search_id: str) -> dict | None:
        """Get the recommendation a queued download's search_id came from.

        Used by DownloadMonitor to recover artist/track intent for a rec
        download at import time (P-MB-1) — recs never get a `searches`
        header row, so this is the only source for it. Returns the full row,
        which includes `source` (P6.7-0b: the category that routes the file
        to its per-category beets profile). Most recent match wins on the
        off chance a search_id is ever reused.
        """
        return self._db.fetch_one(
            "SELECT * FROM recommendations WHERE search_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (search_id,),
        )

    def set_playlist(self, rec_id: int, playlist_id: str) -> None:
        """Link a rec row to the playlist its track was added to (S12).

        Status and processed_at are deliberately untouched — unlike
        update_status, this is bookkeeping on an already-processed row, not
        a state transition.
        """
        self._db.execute(
            "UPDATE recommendations SET playlist_id = ? WHERE id = ?",
            (playlist_id, rec_id),
        )

    def get_recs_for_playlist_rotation(self, playlist_id: str) -> list[dict]:
        """Rec rows whose tracks a playlist rotation may evict.

        P6.7-7: only recs **acquired via Soulseek** (a completed download,
        `download_id IS NOT NULL`) are rotated. Rows still queued,
        in-flight or permanently failed are dropped from consideration —
        they are not in the playlist yet and must never block rotation
        (the failsafe).
        """
        return self._db.fetch_all(
            "SELECT * FROM recommendations "
            "WHERE playlist_id = ? AND download_id IS NOT NULL",
            (playlist_id,),
        )

    def get_unplaylisted_downloaded(self, source: str) -> list[dict]:
        """Downloaded recs of a category whose track is not yet in its
        playlist (playlist_id NULL/empty).

        The per-pull retry pass for the add-on-completion hook: a file that
        wasn't indexable the moment the download completed gets another
        chance here, every pull, until the track shows up in the playlist.
        """
        return self._db.fetch_all(
            "SELECT * FROM recommendations "
            "WHERE source = ? AND status = 'downloaded' "
            "AND (playlist_id IS NULL OR playlist_id = '')",
            (source,),
        )

    # ------------------------------------------------------------------
    # Phase 6.7 category state
    # ------------------------------------------------------------------

    def get_category_state(self, category: str) -> dict:
        """Return persisted cursor state for a recommendation category."""
        row = self._db.fetch_one(
            "SELECT category, offset, last_updated, total_count, warning "
            "FROM rec_category_state WHERE category = ?",
            (category,),
        )
        if row is None:
            return {
                "category": category,
                "offset": 0,
                "last_updated": None,
                "total_count": None,
                "warning": None,
            }
        return row

    def set_category_state(
        self,
        category: str,
        *,
        offset: int,
        last_updated: str | None,
        total_count: int | None,
        warning: str | None,
    ) -> None:
        """Persist a category cursor and its user-visible warning."""
        self._db.execute(
            "INSERT INTO rec_category_state "
            "(category, offset, last_updated, total_count, warning, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(category) DO UPDATE SET "
            "offset = excluded.offset, "
            "last_updated = excluded.last_updated, "
            "total_count = excluded.total_count, "
            "warning = excluded.warning, "
            "updated_at = excluded.updated_at",
            (
                category,
                max(0, int(offset)),
                last_updated,
                total_count,
                warning,
                int(time.time()),
            ),
        )

    def category_warnings(self) -> dict[str, str]:
        """Return non-empty persisted warnings keyed by category."""
        rows = self._db.fetch_all(
            "SELECT category, warning FROM rec_category_state "
            "WHERE warning IS NOT NULL AND warning != ''"
        )
        return {row["category"]: row["warning"] for row in rows}

    @staticmethod
    def _deep_track_key(artist: str, track: str, mbid: str | None) -> str:
        """Build a stable identity key for a Deep Cuts pool row."""
        if mbid:
            return f"mbid:{mbid.casefold()}"
        return f"name:{artist.casefold().strip()}::{track.casefold().strip()}"

    def ingest_deep_cuts_playlist(
        self,
        playlist_id: str,
        *,
        title: str | None,
        playlist_date: str | None,
        tracks: list[dict],
    ) -> bool:
        """Record a new LB playlist and append all of its usable tracks.

        Returns False when the UUID was already ingested. The caller must not
        mark an empty response as ingested: an empty response can also mean
        the per-playlist HTTP request failed.
        """
        now = int(time.time())
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM deep_cuts_playlists WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if existing is not None:
                return False

            conn.execute(
                "INSERT INTO deep_cuts_playlists "
                "(playlist_id, title, playlist_date, ingested_at) VALUES (?, ?, ?, ?)",
                (playlist_id, title, playlist_date, now),
            )
            for track in tracks:
                artist = str(track.get("artist") or "").strip()
                title_value = str(track.get("track") or "").strip()
                if not title_value:
                    continue
                mbid = track.get("mbid")
                mbid = str(mbid).strip() if mbid else None
                conn.execute(
                    "INSERT OR IGNORE INTO deep_cuts_pool "
                    "(playlist_id, track_key, artist, track, album, mbid, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        self._deep_track_key(artist, title_value, mbid),
                        artist,
                        title_value,
                        track.get("album"),
                        mbid,
                        now,
                    ),
                )
        return True

    def is_deep_cuts_playlist_ingested(self, playlist_id: str) -> bool:
        """Return whether a ListenBrainz playlist UUID was already ingested."""
        row = self._db.fetch_one(
            "SELECT 1 FROM deep_cuts_playlists WHERE playlist_id = ?",
            (playlist_id,),
        )
        return row is not None

    def take_deep_cuts_tracks(self, limit: int) -> list[dict]:
        """Return and mark up to ``limit`` unique unserved pool tracks."""
        if limit <= 0:
            return []

        rows = self._db.fetch_all(
            "SELECT id, playlist_id, artist, track, album, mbid, track_key "
            "FROM deep_cuts_pool WHERE served_at IS NULL ORDER BY id"
        )
        selected: list[dict] = []
        seen_keys: set[str] = set()
        ids_to_mark: list[int] = []
        for row in rows:
            if row["track_key"] in seen_keys:
                ids_to_mark.append(row["id"])
                continue
            seen_keys.add(row["track_key"])
            if len(selected) >= limit:
                break
            selected.append(row)
            ids_to_mark.append(row["id"])

        if ids_to_mark:
            placeholders = ",".join("?" for _ in ids_to_mark)
            self._db.execute(
                f"UPDATE deep_cuts_pool SET served_at = ? WHERE id IN ({placeholders})",
                (int(time.time()), *ids_to_mark),
            )
        return selected

    def list_deep_cuts_pool(self, *, unserved_only: bool = False) -> list[dict]:
        """List Deep Cuts pool rows, primarily for diagnostics and tests."""
        where = " WHERE served_at IS NULL" if unserved_only else ""
        return self._db.fetch_all(
            "SELECT * FROM deep_cuts_pool" + where + " ORDER BY id"
        )

    def bulk_update_status(self, from_status: str, to_status: str) -> int:
        """Move every row in `from_status` to `to_status`. Returns rowcount."""
        now = int(time.time())
        cursor = self._db.execute(
            "UPDATE recommendations SET status = ?, processed_at = ? WHERE status = ?",
            (to_status, now, from_status),
        )
        return cursor.rowcount

    def count_recs(self) -> int:
        """Return total number of recommendation rows."""
        row = self._db.fetch_one("SELECT COUNT(*) as cnt FROM recommendations")
        return row["cnt"] if row else 0

    def count_recs_by_status(self) -> dict[str, int]:
        """Return per-status recommendation counts. Only statuses with rows appear."""
        rows = self._db.fetch_all(
            "SELECT status, COUNT(*) AS count FROM recommendations GROUP BY status"
        )
        return {row["status"]: row["count"] for row in rows}

    def list_recs(
        self, status: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        """List recommendation rows, newest first. Optionally filtered by status."""
        cols = (
            "id, source, artist, track, mbid, status, "
            "search_id, download_id, playlist_id, created_at, processed_at"
        )
        if status is not None:
            return self._db.fetch_all(
                f"SELECT {cols} FROM recommendations WHERE status = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        return self._db.fetch_all(
            f"SELECT {cols} FROM recommendations ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    # ------------------------------------------------------------------
    # Worker state (worker_state table, P6.5-4)
    #
    # Generic string key/value storage for worker bookkeeping that must
    # survive restarts — currently the RecPuller's last-run timestamps.
    # ------------------------------------------------------------------

    def set_worker_state(self, key: str, value: str) -> None:
        """Store a worker state value (upsert)."""
        self._db.execute(
            "INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )

    def get_worker_state(self, key: str) -> str | None:
        """Get a worker state value, or None if never set."""
        row = self._db.fetch_one("SELECT value FROM worker_state WHERE key = ?", (key,))
        return row["value"] if row else None
