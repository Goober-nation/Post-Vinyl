"""Requests, their origins and catalog-search history (#86). Never pruned."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    id: int
    user_id: int
    song_id: int | None
    source: str
    origin_id: int | None
    query: str | None
    track_state: str
    created_at: int
    last_used_at: int


@dataclass(frozen=True)
class CatalogSearch:
    id: int
    user_id: int
    query: str
    kind: str
    created_at: int
    last_used_at: int


class RequestsStore:
    def get(self, conn: sqlite3.Connection, request_id: int) -> Request | None:
        row = conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        return Request(**row) if row else None

    def create(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        source: str,
        now: int,
        *,
        song_id: int | None = None,
        origin_id: int | None = None,
        query: str | None = None,
    ) -> Request:
        """A request from anywhere but the Soulseek tab (that's ``record_slskd_query``)."""
        if source == "slskd":
            raise ValueError("typed Soulseek searches go through record_slskd_query")
        row = conn.execute(
            """INSERT INTO requests (user_id, song_id, source, origin_id, query,
                                     created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (user_id, song_id, source, origin_id, query, now, now),
        ).fetchone()
        return Request(**row)

    def record_slskd_query(
        self, conn: sqlite3.Connection, user_id: int, query: str, now: int
    ) -> Request:
        """A typed Soulseek-tab search: no origin; a repeat (any case) bumps the old row."""
        row = conn.execute(
            """INSERT INTO requests (user_id, source, query, created_at, last_used_at)
               VALUES (?, 'slskd', ?, ?, ?)
               ON CONFLICT (user_id, lower(query)) WHERE source = 'slskd'
               DO UPDATE SET last_used_at = excluded.last_used_at
               RETURNING *""",
            (user_id, query, now, now),
        ).fetchone()
        return Request(**row)

    def create_pick(
        self, conn: sqlite3.Connection, user_id: int, song_id: int, now: int
    ) -> Request:
        """A file picked from a typed Soulseek search (#108): its own request,
        ``source='slskd'`` with no query (so the typed-query index skips it)."""
        row = conn.execute(
            """INSERT INTO requests (user_id, song_id, source, created_at, last_used_at)
               VALUES (?, ?, 'slskd', ?, ?) RETURNING *""",
            (user_id, song_id, now, now),
        ).fetchone()
        return Request(**row)

    def slskd_queries(
        self, conn: sqlite3.Connection, user_id: int, limit: int
    ) -> list[Request]:
        """A user's typed Soulseek searches, most recently used first."""
        rows = conn.execute(
            """SELECT * FROM requests
               WHERE user_id = ? AND source = 'slskd' AND query IS NOT NULL
               ORDER BY last_used_at DESC, id DESC LIMIT ?""",
            (user_id, limit),
        )
        return [Request(**r) for r in rows]

    def set_song(self, conn: sqlite3.Connection, request_id: int, song_id: int) -> None:
        conn.execute(
            "UPDATE requests SET song_id = ? WHERE id = ?", (song_id, request_id)
        )

    def set_track_state(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        track_state: str,
        expected: str | None = None,
    ) -> bool:
        """Plain write; which transitions are allowed is core/states.py (milestone 5).

        With ``expected``, only writes while the state is still that one, so a
        change made meanwhile by another service isn't overwritten; returns
        whether a row was written.
        """
        if expected is None:
            cur = conn.execute(
                "UPDATE requests SET track_state = ? WHERE id = ?",
                (track_state, request_id),
            )
        else:
            cur = conn.execute(
                "UPDATE requests SET track_state = ? WHERE id = ? AND track_state = ?",
                (track_state, request_id, expected),
            )
        return cur.rowcount == 1

    def latest_for_song(self, conn: sqlite3.Connection, song_id: int) -> Request | None:
        row = conn.execute(
            "SELECT * FROM requests WHERE song_id = ? ORDER BY last_used_at DESC, id DESC LIMIT 1",
            (song_id,),
        ).fetchone()
        return Request(**row) if row else None

    def in_state(self, conn: sqlite3.Connection, track_state: str) -> list[Request]:
        rows = conn.execute(
            "SELECT * FROM requests WHERE track_state = ? ORDER BY id", (track_state,)
        )
        return [Request(**r) for r in rows]

    def history(
        self, conn: sqlite3.Connection, user_id: int, page: int, page_size: int
    ) -> list[Request]:
        """A user's requests, most recently used first; ``page`` starts at 1."""
        rows = conn.execute(
            """SELECT * FROM requests WHERE user_id = ?
               ORDER BY last_used_at DESC, id DESC LIMIT ? OFFSET ?""",
            (user_id, page_size, (max(page, 1) - 1) * page_size),
        )
        return [Request(**r) for r in rows]

    def count(self, conn: sqlite3.Connection, user_id: int) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0])

    def origin_user(self, conn: sqlite3.Connection, origin_id: int) -> int | None:
        """The user an origin belongs to; None when unknown (or a kind with no user yet)."""
        row = conn.execute(
            """SELECT c.user_id FROM origins o
               JOIN catalog_searches c ON c.id = o.id WHERE o.id = ?""",
            (origin_id,),
        ).fetchone()
        return int(row["user_id"]) if row else None

    # ------------------------------------------------------------ catalog searches

    def record_catalog_search(
        self, conn: sqlite3.Connection, user_id: int, query: str, kind: str, now: int
    ) -> CatalogSearch:
        """A typed catalog search; a repeat (any case, same kind) bumps the old row."""
        row = conn.execute(
            """UPDATE catalog_searches SET last_used_at = ?
               WHERE user_id = ? AND lower(query) = lower(?) AND kind = ?
               RETURNING *""",
            (now, user_id, query, kind),
        ).fetchone()
        if row:
            return CatalogSearch(**row)
        origin = conn.execute(
            "INSERT INTO origins (kind) VALUES ('catalog_search') RETURNING id"
        ).fetchone()
        row = conn.execute(
            """INSERT INTO catalog_searches (id, user_id, query, kind, created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING *""",
            (origin["id"], user_id, query, kind, now, now),
        ).fetchone()
        return CatalogSearch(**row)

    def catalog_history(
        self, conn: sqlite3.Connection, user_id: int, page: int, page_size: int
    ) -> list[CatalogSearch]:
        rows = conn.execute(
            """SELECT * FROM catalog_searches WHERE user_id = ?
               ORDER BY last_used_at DESC, id DESC LIMIT ? OFFSET ?""",
            (user_id, page_size, (max(page, 1) - 1) * page_size),
        )
        return [CatalogSearch(**r) for r in rows]
