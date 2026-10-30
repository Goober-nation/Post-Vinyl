"""slskd backend bookkeeping: searches, attempts per download, bad peers (#86).

slskd owns the search results and transfers; these rows only point at them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SlskdSearch:
    id: str
    request_id: int
    query: str
    strategy: str
    created_at: int


@dataclass(frozen=True)
class SlskdAttempt:
    id: int
    download_id: int
    search_id: str
    rank: int
    username: str
    filename: str
    size: int
    transfer_id: str | None
    outcome: str | None
    started_at: int
    ended_at: int | None


@dataclass(frozen=True)
class SlskdPeer:
    username: str
    failure_count: int
    blocked_at: int | None


class SlskdStore:
    # ------------------------------------------------------------ searches

    def add_search(
        self,
        conn: sqlite3.Connection,
        search_id: str,
        request_id: int,
        query: str,
        strategy: str,
        now: int,
    ) -> SlskdSearch:
        row = conn.execute(
            """INSERT INTO slskd_searches (id, request_id, query, strategy, created_at)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (search_id, request_id, query, strategy, now),
        ).fetchone()
        return SlskdSearch(**row)

    def searches_for_request(
        self, conn: sqlite3.Connection, request_id: int
    ) -> list[SlskdSearch]:
        rows = conn.execute(
            "SELECT * FROM slskd_searches WHERE request_id = ? ORDER BY created_at, rowid",
            (request_id,),
        )
        return [SlskdSearch(**r) for r in rows]

    # ------------------------------------------------------------ attempts

    def add_attempt(
        self,
        conn: sqlite3.Connection,
        download_id: int,
        search_id: str,
        rank: int,
        username: str,
        filename: str,
        size: int,
        now: int,
    ) -> SlskdAttempt:
        row = conn.execute(
            """INSERT INTO slskd_attempts
                   (download_id, search_id, rank, username, filename, size, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (download_id, search_id, rank, username, filename, size, now),
        ).fetchone()
        return SlskdAttempt(**row)

    def set_transfer_id(
        self, conn: sqlite3.Connection, attempt_id: int, transfer_id: str
    ) -> None:
        conn.execute(
            "UPDATE slskd_attempts SET transfer_id = ? WHERE id = ?",
            (transfer_id, attempt_id),
        )

    def end_attempt(
        self, conn: sqlite3.Connection, attempt_id: int, outcome: str, now: int
    ) -> None:
        conn.execute(
            "UPDATE slskd_attempts SET outcome = ?, ended_at = ? WHERE id = ?",
            (outcome, now, attempt_id),
        )

    def attempts_for_download(
        self, conn: sqlite3.Connection, download_id: int
    ) -> list[SlskdAttempt]:
        rows = conn.execute(
            "SELECT * FROM slskd_attempts WHERE download_id = ? ORDER BY id",
            (download_id,),
        )
        return [SlskdAttempt(**r) for r in rows]

    def tried_usernames(self, conn: sqlite3.Connection, download_id: int) -> set[str]:
        """Peers already tried for this download; peer selection skips them. An
        interrupted attempt (slskd's fault, not the peer's) doesn't count."""
        rows = conn.execute(
            """SELECT DISTINCT username FROM slskd_attempts WHERE download_id = ?
               AND outcome IS NOT 'interrupted'""",
            (download_id,),
        )
        return {r["username"] for r in rows}

    def attempt_by_transfer(
        self, conn: sqlite3.Connection, transfer_id: str
    ) -> SlskdAttempt | None:
        row = conn.execute(
            "SELECT * FROM slskd_attempts WHERE transfer_id = ?", (transfer_id,)
        ).fetchone()
        return SlskdAttempt(**row) if row else None

    def ended_attempts(self, conn: sqlite3.Connection) -> list[SlskdAttempt]:
        rows = conn.execute(
            "SELECT * FROM slskd_attempts WHERE outcome IS NOT NULL ORDER BY id"
        )
        return [SlskdAttempt(**r) for r in rows]

    def running_attempts(self, conn: sqlite3.Connection) -> list[SlskdAttempt]:
        rows = conn.execute(
            "SELECT * FROM slskd_attempts WHERE outcome IS NULL ORDER BY id"
        )
        return [SlskdAttempt(**r) for r in rows]

    # ------------------------------------------------------------ peers

    def get_peer(self, conn: sqlite3.Connection, username: str) -> SlskdPeer | None:
        row = conn.execute(
            "SELECT * FROM slskd_peers WHERE username = ?", (username,)
        ).fetchone()
        return SlskdPeer(**row) if row else None

    def record_peer_failure(self, conn: sqlite3.Connection, username: str) -> int:
        """Adds one failure; returns the new count."""
        row = conn.execute(
            """INSERT INTO slskd_peers (username, failure_count) VALUES (?, 1)
               ON CONFLICT (username) DO UPDATE SET failure_count = failure_count + 1
               RETURNING failure_count""",
            (username,),
        ).fetchone()
        return int(row["failure_count"])

    def block_peer(self, conn: sqlite3.Connection, username: str, now: int) -> None:
        conn.execute(
            """INSERT INTO slskd_peers (username, blocked_at) VALUES (?, ?)
               ON CONFLICT (username) DO UPDATE SET blocked_at = excluded.blocked_at""",
            (username, now),
        )

    def peers(self, conn: sqlite3.Connection) -> list[SlskdPeer]:
        """Every peer with a bad record: banned first, then by failures (#112)."""
        rows = conn.execute(
            """SELECT * FROM slskd_peers
               ORDER BY blocked_at IS NULL, blocked_at DESC, failure_count DESC, username"""
        )
        return [SlskdPeer(**r) for r in rows]

    def clear_peers(self, conn: sqlite3.Connection) -> int:
        return conn.execute("DELETE FROM slskd_peers").rowcount

    def clear_peer(self, conn: sqlite3.Connection, username: str) -> None:
        """Back to clean: no row."""
        conn.execute("DELETE FROM slskd_peers WHERE username = ?", (username,))

    def blocked_usernames(
        self, conn: sqlite3.Connection, banned_since: int | None = None
    ) -> set[str]:
        """Blocked peers; with ``banned_since``, older bans are deleted first
        (ban expiry is checked lazily on lookup, core-db-redesign.md)."""
        if banned_since is not None:
            conn.execute(
                "DELETE FROM slskd_peers WHERE blocked_at IS NOT NULL AND blocked_at < ?",
                (banned_since,),
            )
        rows = conn.execute(
            "SELECT username FROM slskd_peers WHERE blocked_at IS NOT NULL"
        )
        return {r["username"] for r in rows}

    def running_for_download(
        self, conn: sqlite3.Connection, download_id: int
    ) -> SlskdAttempt | None:
        row = conn.execute(
            """SELECT * FROM slskd_attempts WHERE download_id = ? AND outcome IS NULL
               ORDER BY id DESC LIMIT 1""",
            (download_id,),
        ).fetchone()
        return SlskdAttempt(**row) if row else None
