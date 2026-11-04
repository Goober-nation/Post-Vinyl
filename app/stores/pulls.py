"""Pulls: named batches of songs that download into one Navidrome playlist.

Each way to pull has its own table, listed in ``PULL_TABLES`` with the columns
every kind shares; its id is its ``origins`` id. ``pull_items`` holds the chosen
songs of any kind and is the queue the pull feeder reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

PULL_TABLES = {"lb_recs": "user_lb_playlists", "lb_radio": "user_radio_requests"}
_ALL_PULLS = " UNION ALL ".join(
    f"""SELECT '{kind}' AS kind, id, user_id, name, state, navidrome_playlist_id,
               created_at FROM {table}"""
    for kind, table in PULL_TABLES.items()
)
_STILL_RUNNING = "('requested', 'reRequested', 'searching', 'found', 'midDownload')"
_BEFORE_DOWNLOAD = "('requested', 'reRequested', 'searching', 'found')"
_INDEXED_SONG = """JOIN file_management f ON f.song_id = i.song_id
                   WHERE f.file_state = 'indexed' AND f.navidrome_id IS NOT NULL"""


@dataclass(frozen=True)
class Pull:
    kind: str
    id: int
    user_id: int
    name: str
    state: str
    navidrome_playlist_id: str | None
    created_at: int


@dataclass(frozen=True)
class PullItem:
    origin_id: int
    position: int
    recording_mbid: str
    title: str | None
    artist: str | None
    state: str
    request_id: int | None
    song_id: int | None
    in_playlist: int


@dataclass(frozen=True)
class NewItem:
    recording_mbid: str
    title: str | None
    artist: str | None


@dataclass(frozen=True)
class LbTrack:
    rank: int
    recording_mbid: str
    score: float
    latest_listened_at: str | None
    title: str | None
    artist: str | None
    artist_mbids: str | None
    album: str | None
    release_mbid: str | None
    length_s: float | None


@dataclass(frozen=True)
class LbList:
    last_updated: int
    pulled_at: int
    songs: int


@dataclass(frozen=True)
class PullCounts:
    total: int
    queued: int
    downloading: int
    in_playlist: int
    failed: int


class PullsStore:
    # ------------------------------------------------------------ pulls

    def create(
        self,
        conn: sqlite3.Connection,
        kind: str,
        user_id: int,
        name: str,
        now: int,
        **details: Any,
    ) -> Pull:
        """``details`` are the kind's own columns."""
        origin = conn.execute(
            "INSERT INTO origins (kind) VALUES (?) RETURNING id", (kind,)
        ).fetchone()
        fields = {"id": origin["id"], "user_id": user_id, "name": name,
                  "created_at": now, **details}  # fmt: skip
        marks = ", ".join("?" for _ in fields)
        conn.execute(
            f"INSERT INTO {PULL_TABLES[kind]} ({', '.join(fields)}) VALUES ({marks})",
            tuple(fields.values()),
        )
        pull = self.get(conn, origin["id"])
        assert pull is not None
        return pull

    def get(self, conn: sqlite3.Connection, pull_id: int) -> Pull | None:
        row = conn.execute(
            f"SELECT * FROM ({_ALL_PULLS}) WHERE id = ?", (pull_id,)
        ).fetchone()
        return Pull(**row) if row else None

    def for_user(self, conn: sqlite3.Connection, user_id: int) -> list[Pull]:
        rows = conn.execute(
            f"SELECT * FROM ({_ALL_PULLS}) WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        )
        return [Pull(**r) for r in rows]

    def running(self, conn: sqlite3.Connection) -> list[Pull]:
        rows = conn.execute(
            f"SELECT * FROM ({_ALL_PULLS}) WHERE state = 'running' ORDER BY id"
        )
        return [Pull(**r) for r in rows]

    def set_state(self, conn: sqlite3.Connection, pull: Pull, state: str) -> None:
        conn.execute(
            f"UPDATE {PULL_TABLES[pull.kind]} SET state = ? WHERE id = ?",
            (state, pull.id),
        )

    def set_playlist(
        self, conn: sqlite3.Connection, pull: Pull, playlist_id: str
    ) -> None:
        conn.execute(
            f"UPDATE {PULL_TABLES[pull.kind]} SET navidrome_playlist_id = ? WHERE id = ?",
            (playlist_id, pull.id),
        )

    # ------------------------------------------------------------ items

    def add_items(
        self, conn: sqlite3.Connection, pull_id: int, items: list[NewItem]
    ) -> None:
        conn.executemany(
            """INSERT INTO pull_items (origin_id, position, recording_mbid, title, artist)
               VALUES (?, ?, ?, ?, ?)""",
            [(pull_id, n, i.recording_mbid, i.title, i.artist)
             for n, i in enumerate(items, 1)],
        )  # fmt: skip

    def items(self, conn: sqlite3.Connection, pull_id: int) -> list[PullItem]:
        rows = conn.execute(
            "SELECT * FROM pull_items WHERE origin_id = ? ORDER BY position", (pull_id,)
        )
        return [PullItem(**r) for r in rows]

    def next_queued(self, conn: sqlite3.Connection, pull_id: int) -> PullItem | None:
        row = conn.execute(
            """SELECT * FROM pull_items WHERE origin_id = ? AND state = 'queued'
               ORDER BY position LIMIT 1""",
            (pull_id,),
        ).fetchone()
        return PullItem(**row) if row else None

    def set_item(
        self,
        conn: sqlite3.Connection,
        item: PullItem,
        state: str,
        song_id: int | None = None,
        request_id: int | None = None,
    ) -> None:
        conn.execute(
            """UPDATE pull_items SET state = ?, song_id = ?, request_id = ?
               WHERE origin_id = ? AND position = ?""",
            (state, song_id, request_id, item.origin_id, item.position),
        )

    def cancel_queued(self, conn: sqlite3.Connection, pull_id: int) -> None:
        conn.execute(
            "UPDATE pull_items SET state = 'cancelled' WHERE origin_id = ? AND state = 'queued'",
            (pull_id,),
        )

    def undownloaded_requests(
        self, conn: sqlite3.Connection, pull_id: int
    ) -> list[int]:
        """The pull's requests that haven't reached a download yet."""
        rows = conn.execute(
            f"""SELECT r.id FROM pull_items i JOIN requests r ON r.id = i.request_id
                WHERE i.origin_id = ? AND r.track_state IN {_BEFORE_DOWNLOAD}""",
            (pull_id,),
        )
        return [r["id"] for r in rows]

    def user_undownloaded_requests(
        self, conn: sqlite3.Connection, user_id: int
    ) -> list[int]:
        """Any of the user's requests that haven't reached a download yet."""
        rows = conn.execute(
            f"""SELECT id FROM requests
                WHERE user_id = ? AND track_state IN {_BEFORE_DOWNLOAD}""",
            (user_id,),
        )
        return [r["id"] for r in rows]

    def is_settled(self, conn: sqlite3.Connection, pull_id: int) -> bool:
        """Nothing queued, and every request it made has ended."""
        row = conn.execute(
            f"""SELECT 1 FROM pull_items i LEFT JOIN requests r ON r.id = i.request_id
                WHERE i.origin_id = ?
                  AND (i.state = 'queued' OR r.track_state IN {_STILL_RUNNING})
                LIMIT 1""",
            (pull_id,),
        ).fetchone()
        return row is None

    def counts(self, conn: sqlite3.Connection, pull_id: int) -> PullCounts:
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                       COALESCE(SUM(i.state = 'queued'), 0) AS queued,
                       COALESCE(SUM(r.track_state IN {_STILL_RUNNING}), 0) AS downloading,
                       COALESCE(SUM(i.in_playlist), 0) AS in_playlist,
                       COALESCE(SUM(i.state = 'failed' OR r.track_state IN
                           ('notFound', 'notDownloaded', 'badDownload')), 0) AS failed
                FROM pull_items i LEFT JOIN requests r ON r.id = i.request_id
                WHERE i.origin_id = ?""",
            (pull_id,),
        ).fetchone()
        return PullCounts(**row)

    def user_requests_waiting(self, conn: sqlite3.Connection) -> bool:
        """A request (a user's or a pull's) hasn't reached a download yet."""
        row = conn.execute(
            f"""SELECT 1 FROM requests
                WHERE song_id IS NOT NULL AND track_state IN {_BEFORE_DOWNLOAD} LIMIT 1"""
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ playlists

    def pulls_to_sync(self, conn: sqlite3.Connection) -> list[int]:
        """Pulls with an indexed song their playlist doesn't have yet."""
        rows = conn.execute(
            f"""SELECT DISTINCT i.origin_id FROM pull_items i {_INDEXED_SONG}
                AND i.in_playlist = 0 ORDER BY i.origin_id"""
        )
        return [r["origin_id"] for r in rows]

    def indexed_songs(self, conn: sqlite3.Connection, pull_id: int) -> list[str]:
        """The pull's playable songs as Navidrome ids, in pull order."""
        rows = conn.execute(
            f"""SELECT f.navidrome_id FROM pull_items i {_INDEXED_SONG}
                AND i.origin_id = ? ORDER BY i.position""",
            (pull_id,),
        )
        return [r["navidrome_id"] for r in rows]

    def mark_in_playlist(self, conn: sqlite3.Connection, pull_id: int) -> None:
        conn.execute(
            """UPDATE pull_items SET in_playlist = 1
                WHERE origin_id = ? AND song_id IN
                    (SELECT song_id FROM file_management
                     WHERE file_state = 'indexed' AND navidrome_id IS NOT NULL)""",
            (pull_id,),
        )

    # ------------------------------------------------------------ ListenBrainz lists

    def has_lb_list(
        self, conn: sqlite3.Connection, user_id: int, last_updated: int
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM lb_playlists WHERE user_id = ? AND last_updated = ? LIMIT 1",
            (user_id, last_updated),
        ).fetchone()
        return row is not None

    def add_lb_list(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        last_updated: int,
        pulled_at: int,
        tracks: list[LbTrack],
    ) -> None:
        conn.executemany(
            """INSERT INTO lb_playlists (user_id, last_updated, pulled_at, rank,
                   recording_mbid, score, latest_listened_at, title, artist,
                   artist_mbids, album, release_mbid, length_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(user_id, last_updated, pulled_at, t.rank, t.recording_mbid, t.score,
              t.latest_listened_at, t.title, t.artist, t.artist_mbids, t.album,
              t.release_mbid, t.length_s) for t in tracks],
        )  # fmt: skip

    def lb_lists(self, conn: sqlite3.Connection, user_id: int) -> list[LbList]:
        rows = conn.execute(
            """SELECT last_updated, MIN(pulled_at) AS pulled_at, COUNT(*) AS songs
               FROM lb_playlists WHERE user_id = ?
               GROUP BY last_updated ORDER BY last_updated DESC""",
            (user_id,),
        )
        return [LbList(**r) for r in rows]

    def lb_tracks(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        last_updated: int,
        first_rank: int,
        last_rank: int,
    ) -> list[LbTrack]:
        rows = conn.execute(
            """SELECT rank, recording_mbid, score, latest_listened_at, title, artist,
                      artist_mbids, album, release_mbid, length_s
               FROM lb_playlists
               WHERE user_id = ? AND last_updated = ? AND rank BETWEEN ? AND ?
               ORDER BY rank""",
            (user_id, last_updated, first_rank, last_rank),
        )
        return [LbTrack(**r) for r in rows]
