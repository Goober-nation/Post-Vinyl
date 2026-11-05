"""Thin client for Navidrome's Subsonic and native APIs (#80).

Checked against Navidrome 0.64.2's source and live (2026-10-28):
- ``POST /auth/login`` returns a native-API JWT plus a Subsonic token
  (md5(password + salt)) and its salt, so no call after login needs the
  password. Login is rate-limited (5 per 20 s by default): reuse the session.
- Every native-API response carries a refreshed JWT in ``X-ND-Authorization``.
- Subsonic answers errors with HTTP 200 and ``status: "failed"``.
- Subsonic's ``path`` is built from tags; only the native API has the real one.
- No API says whether any user exists: only ``/app/``'s embedded config does.
  ``POST /auth/createAdmin`` needs no login and answers 403 once a user exists.
- ``POST /api/user`` (admin) takes ``userName``, ``name``, ``password`` and
  ``isAdmin``, answers ``{id}``, and accepts an empty username: check it first.
  A taken name is 400 ``ra.validation.unique``. New users get the libraries.
- ``PUT /api/user/{id}`` replaces the whole user: a partial body blanks the name.
  Changing your own password also needs ``currentPassword``.
- ``DELETE /api/user/{id}`` lets an admin delete themselves: the caller prevents it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import requests

_AUTH_HEADER = "X-ND-Authorization"
_SUBSONIC_VERSION = "1.16.1"
_CLIENT_NAME = "post-vinyl"
# Subsonic error codes (subsonic.org/pages/api.jsp)
_SUBSONIC_AUTH_CODES = {40, 41, 44}
_SUBSONIC_NOT_FOUND = 70
# In the config Navidrome embeds in its page, with its quotes escaped (0.64.2)
_FIRST_TIME = re.compile(r'firstTime\\*":\s*true')


class NavidromeError(Exception):
    """A Navidrome call failed: connection error, timeout or unexpected status."""


class NavidromeUnreachable(NavidromeError):
    """No answer at all: connection refused, DNS failure or timeout."""


class NavidromeAuthError(NavidromeError):
    """Wrong credentials, or an expired/revoked session: log in again."""


class NavidromeNotFound(NavidromeError):
    """Unknown id."""


class NavidromeForbidden(NavidromeError):
    """Not allowed: e.g. createAdmin once a user exists, or startScan as a
    non-admin."""


class NavidromeDuplicate(NavidromeError):
    """The username is already taken."""


class NavidromeBusy(NavidromeError):
    """Rate-limited (HTTP 429), e.g. too many logins; retry later."""


@dataclass
class Session:
    """A logged-in user. ``jwt`` is replaced with each refreshed token."""

    username: str
    is_admin: bool
    jwt: str
    subsonic_token: str
    subsonic_salt: str
    nd_id: str = ""  # Navidrome's permanent user id


@dataclass(frozen=True)
class ScanStatus:
    scanning: bool
    # while scanning: songs/folders scanned so far (0 when a scan starts);
    # otherwise the library's totals
    count: int
    folder_count: int
    last_scan: str | None  # ISO timestamp, None before the first scan
    error: str  # last scan's error, "" when none


@dataclass(frozen=True)
class Song:
    id: str
    path: str  # real path, relative to library_path
    library_path: str
    title: str
    artist: str
    album: str
    duration: float  # seconds
    size: int
    suffix: str
    missing: bool  # file gone from disk but still in the index
    has_cover: bool = False  # Navidrome found art for it (embedded or a folder image)
    bitrate: int = 0  # kbps; 0 when unknown
    release_id: str = ""  # MusicBrainz release, "" when untagged


@dataclass(frozen=True)
class Cover:
    data: bytes
    content_type: str


class NavidromeClientBase(ABC):
    @abstractmethod
    def login(self, username: str, password: str) -> Session:
        """Log in; raises NavidromeAuthError on wrong credentials."""

    @abstractmethod
    def has_no_users(self) -> bool:
        """True on a fresh Navidrome, before its first admin exists."""

    @abstractmethod
    def create_first_admin(self, username: str, password: str) -> Session:
        """Create the first admin and log in; NavidromeForbidden once a user exists."""

    @abstractmethod
    def create_user(
        self, session: Session, username: str, password: str, is_admin: bool
    ) -> str:
        """Create an account (admin only), returning its id; NavidromeDuplicate
        if the name is taken."""

    @abstractmethod
    def set_password(
        self,
        session: Session,
        nd_id: str | None,
        username: str,
        password: str,
        current: str | None = None,
    ) -> None:
        """Set a password (admin only); ``current`` is needed for your own.
        NavidromeNotFound if no such account."""

    @abstractmethod
    def delete_user(self, session: Session, nd_id: str | None, username: str) -> None:
        """Delete an account (admin only); NavidromeNotFound if it's already gone."""

    @abstractmethod
    def link_listenbrainz(self, session: Session, token: str | None) -> None:
        """Set the session user's scrobbling token; ``None`` unlinks it."""

    @abstractmethod
    def start_scan(self, session: Session, full: bool = False) -> ScanStatus:
        """Start a library scan (admin only); returns the status once the scan
        has begun (Navidrome waits up to 3 s for that)."""

    @abstractmethod
    def scan_status(self, session: Session) -> ScanStatus: ...

    @abstractmethod
    def get_song(self, session: Session, song_id: str) -> Song: ...

    @abstractmethod
    def list_songs(
        self, session: Session, query: str, start: int, end: int
    ) -> tuple[list[Song], int]:
        """The library, by title; ``query`` matches title, album and artists.
        Returns rows ``start``..``end`` (exclusive) and the total."""

    @abstractmethod
    def cover_art(self, session: Session, song_id: str, size: int) -> Cover:
        """The song's cover, ``size`` px on its longer side; NavidromeNotFound
        when Navidrome has none for that id (#126)."""

    @abstractmethod
    def save_playlist(
        self, session: Session, name: str, song_ids: list[str], playlist_id: str | None
    ) -> str:
        """Create a playlist, or replace ``playlist_id``'s songs; returns its id."""

    @abstractmethod
    def marks(self, session: Session, song_id: str) -> tuple[bool, int]:
        """The session's own (starred, rating 0-5) on a song."""

    @abstractmethod
    def set_marks(
        self, session: Session, song_id: str, starred: bool, rating: int
    ) -> None:
        """Star a song and rate it, as the session's own user."""

    @abstractmethod
    def find_song_by_path(self, session: Session, path: str) -> Song | None:
        """The song at exactly ``path`` (relative to its library), or None."""


def parse_scan_status(raw: dict[str, Any]) -> ScanStatus:
    return ScanStatus(
        scanning=bool(raw.get("scanning")),
        count=raw.get("count", 0),
        folder_count=raw.get("folderCount", 0),
        # Go's zero time before the first scan (live 2026-10-28)
        last_scan=None
        if (raw.get("lastScan") or "").startswith("0001-01-01")
        else raw.get("lastScan"),
        error=raw.get("error") or "",
    )


def parse_song(raw: dict[str, Any]) -> Song:
    return Song(
        id=raw["id"],
        path=raw.get("path", ""),
        library_path=raw.get("libraryPath", ""),
        title=raw.get("title", ""),
        artist=raw.get("artist", ""),
        album=raw.get("album", ""),
        duration=float(raw.get("duration") or 0),
        size=raw.get("size", 0),
        suffix=raw.get("suffix", ""),
        missing=bool(raw.get("missing")),
        has_cover=bool(raw.get("hasCoverArt")),
        bitrate=int(raw.get("bitRate") or 0),
        release_id=raw.get("mbzAlbumId") or "",
    )


def parse_session(raw: dict[str, Any]) -> Session:
    return Session(
        username=raw["username"],
        is_admin=bool(raw.get("isAdmin")),
        jwt=raw["token"],
        subsonic_token=raw["subsonicToken"],
        subsonic_salt=raw["subsonicSalt"],
        nd_id=raw.get("id", ""),
    )


class NavidromeClient(NavidromeClientBase):
    def __init__(self, base_url: str, timeout_s: float = 15) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._http = requests.Session()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            resp = self._http.request(
                method, self._base + path, timeout=self._timeout, **kwargs
            )
        except requests.RequestException as e:
            raise NavidromeUnreachable(f"{method} {path}: {e}") from e
        error = {
            401: NavidromeAuthError,
            403: NavidromeForbidden,
            404: NavidromeNotFound,
            429: NavidromeBusy,
        }.get(resp.status_code)
        if error:
            raise error(f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        if not resp.ok:
            raise NavidromeError(
                f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp

    def _auth_call(self, path: str, username: str, password: str) -> Session:
        body = {"username": username, "password": password}
        return parse_session(self._request("POST", path, json=body).json())

    def _subsonic(self, session: Session, endpoint: str, **params: Any) -> Any:
        params |= self._auth_params(session)
        resp = self._request("GET", f"/rest/{endpoint}", params=params)
        return self._checked(endpoint, resp.json()["subsonic-response"])

    def _auth_params(self, session: Session) -> dict[str, Any]:
        return {
            "u": session.username,
            "t": session.subsonic_token,
            "s": session.subsonic_salt,
            "v": _SUBSONIC_VERSION,
            "c": _CLIENT_NAME,
            "f": "json",
        }

    @staticmethod
    def _checked(endpoint: str, body: dict[str, Any]) -> Any:
        """Subsonic answers its errors as HTTP 200 with ``status: failed``."""
        if body.get("status") != "ok":
            err = body.get("error") or {}
            code = err.get("code")
            msg = f"/rest/{endpoint}: subsonic error {code} {err.get('message', '')}"
            if code in _SUBSONIC_AUTH_CODES:
                raise NavidromeAuthError(msg)
            if code == 50:
                raise NavidromeForbidden(msg)
            if code == _SUBSONIC_NOT_FOUND:
                raise NavidromeNotFound(msg)
            raise NavidromeError(msg)
        return body

    def cover_art(self, session: Session, song_id: str, size: int) -> Cover:
        # 0.64.2 (live 2026-10-31): the song id works as is; an unknown id is
        # HTTP 200 with a JSON error (code 70), an image is image/*
        params = {"id": song_id, "size": size} | self._auth_params(session)
        resp = self._request("GET", "/rest/getCoverArt", params=params)
        kind = resp.headers.get("Content-Type", "")
        if not kind.startswith("image/"):
            self._checked("getCoverArt", resp.json()["subsonic-response"])
            raise NavidromeError(f"/rest/getCoverArt: not an image ({kind})")
        return Cover(resp.content, kind)

    def _native_response(
        self, session: Session, endpoint: str, **params: Any
    ) -> requests.Response:
        resp = self._request(
            "GET",
            f"/api{endpoint}",
            params=params,
            headers={_AUTH_HEADER: f"Bearer {session.jwt}"},
        )
        refreshed = resp.headers.get(_AUTH_HEADER)
        if refreshed:
            session.jwt = refreshed
        return resp

    def _native(self, session: Session, endpoint: str, **params: Any) -> Any:
        return self._native_response(session, endpoint, **params).json()

    def login(self, username: str, password: str) -> Session:
        return self._auth_call("/auth/login", username, password)

    def has_no_users(self) -> bool:
        return bool(_FIRST_TIME.search(self._request("GET", "/app/").text))

    def create_first_admin(self, username: str, password: str) -> Session:
        return self._auth_call("/auth/createAdmin", username, password)

    def create_user(
        self, session: Session, username: str, password: str, is_admin: bool
    ) -> str:
        body = {
            "userName": username,
            "name": username,
            "password": password,
            "isAdmin": is_admin,
        }
        headers = {_AUTH_HEADER: f"Bearer {session.jwt}"}
        try:
            return str(
                self._request("POST", "/api/user", json=body, headers=headers).json()[
                    "id"
                ]
            )
        except NavidromeError as e:
            if "ra.validation.unique" in str(e):
                raise NavidromeDuplicate(str(e)) from e
            raise

    def _user_record(
        self, session: Session, nd_id: str | None, username: str
    ) -> dict[str, Any]:
        for raw in self._native(session, "/user"):
            if raw["id"] == nd_id or (not nd_id and raw["userName"] == username):
                return dict(raw)
        raise NavidromeNotFound(f"no Navidrome user {username}")

    def set_password(
        self,
        session: Session,
        nd_id: str | None,
        username: str,
        password: str,
        current: str | None = None,
    ) -> None:
        record = self._user_record(session, nd_id, username)
        body = record | {"password": password}
        if current is not None:
            body["currentPassword"] = current
        self._request(
            "PUT",
            f"/api/user/{record['id']}",
            json=body,
            headers={_AUTH_HEADER: f"Bearer {session.jwt}"},
        )

    def delete_user(self, session: Session, nd_id: str | None, username: str) -> None:
        record = self._user_record(session, nd_id, username)
        self._request(
            "DELETE",
            f"/api/user/{record['id']}",
            headers={_AUTH_HEADER: f"Bearer {session.jwt}"},
        )

    def link_listenbrainz(self, session: Session, token: str | None) -> None:
        # Acts on the JWT's own user, so it can't be done with the admin's session.
        # Only mounted while ListenBrainz.Enabled is on in Navidrome.
        self._request(
            "PUT" if token else "DELETE",
            "/api/listenbrainz/link",
            json={"token": token} if token else None,
            headers={_AUTH_HEADER: f"Bearer {session.jwt}"},
        )

    def start_scan(self, session: Session, full: bool = False) -> ScanStatus:
        body = self._subsonic(session, "startScan", fullScan=str(full).lower())
        return parse_scan_status(body["scanStatus"])

    def scan_status(self, session: Session) -> ScanStatus:
        return parse_scan_status(self._subsonic(session, "getScanStatus")["scanStatus"])

    def get_song(self, session: Session, song_id: str) -> Song:
        return parse_song(self._native(session, f"/song/{song_id}"))

    def list_songs(
        self, session: Session, query: str, start: int, end: int
    ) -> tuple[list[Song], int]:
        # 0.64.2: the "title" filter is full text (title, album, artists);
        # the total is in X-Total-Count
        params: dict[str, Any] = {"_start": start, "_end": end, "_sort": "title",
                                  "_order": "ASC"}  # fmt: skip
        if query:
            params["title"] = query
        resp = self._native_response(session, "/song", **params)
        rows = resp.json() or []
        return [parse_song(r) for r in rows], int(
            resp.headers.get("X-Total-Count", len(rows))
        )

    def marks(self, session: Session, song_id: str) -> tuple[bool, int]:
        song = self._subsonic(session, "getSong", id=song_id)["song"]
        return bool(song.get("starred")), int(song.get("userRating") or 0)

    def set_marks(
        self, session: Session, song_id: str, starred: bool, rating: int
    ) -> None:
        if starred:
            self._subsonic(session, "star", id=song_id)
        if rating:
            self._subsonic(session, "setRating", id=song_id, rating=rating)

    def find_song_by_path(self, session: Session, path: str) -> Song | None:
        # Navidrome's path filter is a prefix match; keep exact hits only.
        rows = self._native(session, "/song", path=path, _start=0, _end=100)
        exact = [parse_song(r) for r in rows if r.get("path") == path]
        if len(exact) > 1:
            raise NavidromeError(f"{len(exact)} songs at path {path!r}")
        return exact[0] if exact else None

    def save_playlist(
        self, session: Session, name: str, song_ids: list[str], playlist_id: str | None
    ) -> str:
        form: dict[str, Any] = {"songId": song_ids, **self._auth_params(session)}
        form |= {"playlistId": playlist_id} if playlist_id else {"name": name}
        # POST: a full playlist doesn't fit in a URL
        resp = self._request("POST", "/rest/createPlaylist", data=form)
        body = self._checked("createPlaylist", resp.json()["subsonic-response"])
        return str(body["playlist"]["id"])
