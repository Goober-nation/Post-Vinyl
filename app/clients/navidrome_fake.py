"""In-memory Navidrome client for tests of code built on the Navidrome client."""

from __future__ import annotations

from app.clients.navidrome import (
    Cover,
    NavidromeAuthError,
    NavidromeClientBase,
    NavidromeDuplicate,
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
        self.covers: dict[str, bytes] = {}  # song id -> image (JPEG bytes)
        self.user_marks: dict[tuple[str, str], tuple[bool, int]] = {}
        self.playlists: dict[
            str, tuple[str, str, list[str]]
        ] = {}  # id -> owner, name, songs
        self.lb_links: dict[str, str] = {}  # username -> scrobbling token

    def _session(self, username: str) -> Session:
        return Session(
            username=username,
            is_admin=self.users[username][1],
            jwt=f"jwt-{username}",
            subsonic_token="token",
            subsonic_salt="salt",
            nd_id=f"nd-{username}",
        )

    def _check(self, session: Session) -> None:
        if session.username not in self.users:
            raise NavidromeAuthError(f"unknown user {session.username}")

    def login(self, username: str, password: str) -> Session:
        if self.users.get(username, ("", False))[0] != password or not password:
            raise NavidromeAuthError("POST /auth/login: HTTP 401")
        return self._session(username)

    def has_no_users(self) -> bool:
        return not self.users

    def create_first_admin(self, username: str, password: str) -> Session:
        if self.users:
            raise NavidromeForbidden("POST /auth/createAdmin: HTTP 403")
        self.users[username] = (password, True)
        return self._session(username)

    def create_user(
        self, session: Session, username: str, password: str, is_admin: bool
    ) -> str:
        self._check(session)
        if not session.is_admin:
            raise NavidromeForbidden("POST /api/user: HTTP 403")
        if username in self.users:
            raise NavidromeDuplicate("POST /api/user: HTTP 400")
        self.users[username] = (password, is_admin)
        return f"nd-{username}"

    def set_password(
        self,
        session: Session,
        nd_id: str | None,
        username: str,
        password: str,
        current: str | None = None,
    ) -> None:
        self._check(session)
        if username not in self.users:
            raise NavidromeNotFound(username)
        self.users[username] = (password, self.users[username][1])

    def delete_user(self, session: Session, nd_id: str | None, username: str) -> None:
        self._check(session)
        if username not in self.users:
            raise NavidromeNotFound(username)
        del self.users[username]

    def link_listenbrainz(self, session: Session, token: str | None) -> None:
        self._check(session)
        if token:
            self.lb_links[session.username] = token
        else:
            self.lb_links.pop(session.username, None)

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

    def cover_art(self, session: Session, song_id: str, size: int) -> Cover:
        self._check(session)
        if song_id not in self.covers:
            raise NavidromeNotFound(f"/rest/getCoverArt: subsonic error 70 {song_id}")
        return Cover(self.covers[song_id], "image/jpeg")

    def marks(self, session: Session, song_id: str) -> tuple[bool, int]:
        self._check(session)
        return self.user_marks.get((session.username, song_id), (False, 0))

    def set_marks(
        self, session: Session, song_id: str, starred: bool, rating: int
    ) -> None:
        self._check(session)
        self.user_marks[(session.username, song_id)] = (starred, rating)

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

    def save_playlist(
        self, session: Session, name: str, song_ids: list[str], playlist_id: str | None
    ) -> str:
        self._check(session)
        if playlist_id and playlist_id not in self.playlists:
            raise NavidromeNotFound(f"playlist {playlist_id}")
        pid = playlist_id or f"pl-{len(self.playlists) + 1}"
        self.playlists[pid] = (session.username, name, list(song_ids))
        return pid
