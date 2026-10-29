"""Songs and their source ids (#86). One song per real track, shared by all users."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Song:
    id: int
    title: str
    artist_name: str
    query_title: str | None
    main_artist: str | None
    album_title: str | None
    track_number: int | None
    disc_number: int | None
    disc_count: int | None
    duration_s: int | None


@dataclass(frozen=True)
class SongFields:
    """A song's metadata before it has an id."""

    title: str
    artist_name: str
    query_title: str | None = None
    main_artist: str | None = None
    album_title: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    disc_count: int | None = None
    duration_s: int | None = None


class IdentityConflict(Exception):
    """The given ids already point at more than one song."""


class SongsStore:
    def get(self, conn: sqlite3.Connection, song_id: int) -> Song | None:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return Song(**row) if row else None

    def find_by_identity(
        self, conn: sqlite3.Connection, source: str, source_id: str
    ) -> Song | None:
        row = conn.execute(
            """SELECT s.* FROM songs s JOIN song_identity i ON i.song_id = s.id
               WHERE i.source = ? AND i.source_id = ?""",
            (source, source_id),
        ).fetchone()
        return Song(**row) if row else None

    def identities(self, conn: sqlite3.Connection, song_id: int) -> dict[str, str]:
        rows = conn.execute(
            "SELECT source, source_id FROM song_identity WHERE song_id = ?", (song_id,)
        )
        return {r["source"]: r["source_id"] for r in rows}

    def create(self, conn: sqlite3.Connection, fields: SongFields) -> Song:
        """A song with no identities (a Soulseek-tab file that matched nothing)."""
        row = conn.execute(
            """INSERT INTO songs (title, artist_name, query_title, main_artist,
                                  album_title, track_number, disc_number, disc_count,
                                  duration_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (
                fields.title,
                fields.artist_name,
                fields.query_title,
                fields.main_artist,
                fields.album_title,
                fields.track_number,
                fields.disc_number,
                fields.disc_count,
                fields.duration_s,
            ),
        ).fetchone()
        return Song(**row)

    def find_or_create(
        self,
        conn: sqlite3.Connection,
        fields: SongFields,
        identities: dict[str, str],
    ) -> Song:
        """The song any of ``identities`` already points at, else a new one.

        Ids the song doesn't have yet are added. Raises IdentityConflict when
        the ids point at two different songs (the caller decides what's right).
        """
        found: dict[int, Song] = {}
        for source, source_id in identities.items():
            song = self.find_by_identity(conn, source, source_id)
            if song:
                found[song.id] = song
        if len(found) > 1:
            raise IdentityConflict(f"ids {identities} point at songs {sorted(found)}")
        song = next(iter(found.values())) if found else self.create(conn, fields)
        conn.executemany(
            """INSERT INTO song_identity (song_id, source, source_id) VALUES (?, ?, ?)
               ON CONFLICT DO NOTHING""",
            [(song.id, s, sid) for s, sid in identities.items()],
        )
        return song
