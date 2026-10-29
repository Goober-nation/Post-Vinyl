"""In-memory Navidrome client for tests of code built on the Navidrome client."""

from __future__ import annotations

from app.clients.navidrome import (
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeForbidden,
    NavidromeNotFound,
    ScanStatus,
    Session,
    Song,
)


class FakeNavidromeClient(NavidromeClientBase):
    """``users`` maps username -> (password, is_admin). Songs placed in
    ``pending`` become visible in ``songs`` on the next start_scan()."""

    def __init__(self, users: dict[str, tuple[str, bool]] | None = None) -> None:
        self.users = users or {}
        self.songs: dict[str, Song] = {}
        self.pending: list[Song] = []
        self.scans = 0

    def _session(self, username: str) -> Session:
        return Session(
            username=username,
            is_admin=self.users[username][1],
            jwt=f"jwt-{username}",
            subsonic_token="token",
            subsonic_salt="salt",
        )

    def _check(self, session: Session) -> None:
        if session.username not in self.users:
            raise NavidromeAuthError(f"unknown user {session.username}")

    def login(self, username: str, password: str) -> Session:
        if self.users.get(username, ("", False))[0] != password or not password:
            raise NavidromeAuthError("POST /auth/login: HTTP 401")
        return self._session(username)

    def create_admin(self, username: str, password: str) -> Session:
        if self.users:
            raise NavidromeForbidden("Cannot create another first admin")
        self.users[username] = (password, True)
        return self._session(username)

    def start_scan(self, session: Session, full: bool = False) -> ScanStatus:
        self._check(session)
        if not session.is_admin:
            raise NavidromeForbidden("startScan: subsonic error 50")
        for song in self.pending:
            self.songs[song.id] = song
        self.pending = []
        self.scans += 1
        return self.scan_status(session)

    def scan_status(self, session: Session) -> ScanStatus:
        self._check(session)
        return ScanStatus(
            scanning=False,
            count=len(self.songs),
            folder_count=0,
            last_scan="2026-10-28T00:00:00Z" if self.scans else None,
            error="",
        )

    def get_song(self, session: Session, song_id: str) -> Song:
        self._check(session)
        if song_id not in self.songs:
            raise NavidromeNotFound(f"GET /api/song/{song_id}: HTTP 404")
        return self.songs[song_id]

    def find_song_by_path(self, session: Session, path: str) -> Song | None:
        self._check(session)
        return next((s for s in self.songs.values() if s.path == path), None)

    def list_songs(
        self, session: Session, query: str, start: int, end: int
    ) -> tuple[list[Song], int]:
        self._check(session)
        q = query.lower()
        hits = sorted(
            (s for s in self.songs.values()
             if q in f"{s.title} {s.album} {s.artist}".lower()),
            key=lambda s: s.title.lower(),
        )  # fmt: skip
        return hits[start:end], len(hits)
