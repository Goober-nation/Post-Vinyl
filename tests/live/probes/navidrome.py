"""
`NavidromeProbe` — Navidrome over the Subsonic REST API, from the outside.

Written **independently of `app/services/navidrome_library.py`** on purpose.
If the probe and the service shared a bug — the same wrong parameter name,
the same misread response envelope — every test built on it would pass while
proving nothing at all. So this module talks to `/rest/*` directly, parses
the envelope itself, and agrees with the service only where Navidrome forces
it to.

Two things learned against the live server (Navidrome 0.63.2), both of which
change how the fixtures have to be written:

1. **`startScan` returns before anything is scanned, and `getScanStatus`
   goes `scanning: true -> false` in about a second even for a full scan.**
   The signal that the scan actually *ran* is `lastScan` changing, not
   `scanning` going false, so `trigger_scan` waits for both.
2. **A completed scan does not mean a new file is queryable.** Navidrome's
   filesystem *watcher* fires its own selective scan a few seconds later,
   and on this stack that watcher — not the on-demand scan — is what
   actually imported the tracks. So `wait_for_song()` exists, and any test
   asserting "the file is now in Navidrome" should use it rather than
   assuming `trigger_scan()` was enough.

Also note `song.path` in a Subsonic response is **synthesised from tags**
(`MF Doom/Madvillainy/02-20 - ALL CAPS.flac` for a file that lives under
`downloads/Loupitour462/...`). It is not a filesystem path and must never be
used to locate a file — that is `FsProbe`'s job.
"""

from __future__ import annotations

import hashlib
import random
import string
import time
from typing import Any

import requests

from tests.live.probes.contract import NavidromeProbe
from tests.live.probes.naming import artist_matches, text_key
from tests.live.probes.paths import env_value

DEFAULT_NAVIDROME_URL = "http://localhost:8090"
SUBSONIC_VERSION = "1.16.1"
CLIENT_NAME = "musica-live-probe"


class NavidromeProbeError(RuntimeError):
    """Navidrome answered, but with a Subsonic error."""


class LiveNavidromeProbe(NavidromeProbe):
    def __init__(
        self,
        base_url: str = DEFAULT_NAVIDROME_URL,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or env_value("NAVIDROME_USERNAME")
        self.password = password or env_value("NAVIDROME_PASSWORD")
        self.timeout = timeout
        self._session = requests.Session()

    # -- plumbing ----------------------------------------------------------

    def _auth(self) -> dict:
        """Subsonic token auth: md5(password + salt), fresh salt per call."""
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": SUBSONIC_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    def _call(self, endpoint: str, retries: int = 3, **params: Any) -> dict:
        """One Subsonic call, returning the `subsonic-response` body.

        Connection errors are retried: another agent is restarting
        containers concurrently, and a probe that dies because Navidrome
        was down for two seconds reports a system failure that isn't one.
        A Subsonic-level error is *not* retried — that is an answer.
        """
        url = f"{self.base_url}/rest/{endpoint}"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self._session.get(
                    url, params={**self._auth(), **params}, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last = exc
                self._session.close()
                time.sleep(1.0 + attempt)
                continue
            if resp.status_code >= 500:
                last = NavidromeProbeError(f"{endpoint}: HTTP {resp.status_code}")
                time.sleep(1.0 + attempt)
                continue
            try:
                return resp.json().get("subsonic-response", {})
            except ValueError as exc:
                raise NavidromeProbeError(
                    f"{endpoint}: unparseable response {resp.text[:200]!r}"
                ) from exc
        raise NavidromeProbeError(f"{endpoint}: unreachable after {retries} tries ({last})")

    @staticmethod
    def _ok(body: dict, endpoint: str, *, strict: bool = True) -> dict:
        if body.get("status") == "ok":
            return body
        error = body.get("error", {})
        message = f"{endpoint}: Subsonic error {error.get('code')} {error.get('message')}"
        if strict:
            raise NavidromeProbeError(message)
        return {}

    # -- health ------------------------------------------------------------

    def is_up(self) -> bool:
        try:
            return self._call("ping").get("status") == "ok"
        except (NavidromeProbeError, requests.RequestException):
            return False

    def wait_until_up(self, timeout: float = 120.0) -> float:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.is_up():
                return time.monotonic() - start
            time.sleep(2.0)
        raise TimeoutError(f"Navidrome did not answer within {timeout}s")

    # -- scanning ----------------------------------------------------------

    def scan_status(self) -> dict:
        body = self._call("getScanStatus")
        return body.get("scanStatus", {})

    def trigger_scan(self, wait: bool = True, timeout: float = 180.0) -> bool:
        """Start a scan; when `wait`, block until it has actually finished.

        Returns True when the scan completed (or was started, if
        `wait=False`), False on timeout. See the module docstring for why
        completion is judged on `lastScan` changing rather than on
        `scanning` going false — and for why a completed scan still does not
        guarantee a given file is queryable yet.
        """
        before = self.scan_status().get("lastScan")
        self._ok(self._call("startScan", fullScan="false"), "startScan")
        if not wait:
            return True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.scan_status()
            if not status.get("scanning") and status.get("lastScan") != before:
                return True
            time.sleep(1.0)
        return False

    def full_scan(self, wait: bool = True, timeout: float = 600.0) -> bool:
        """`fullScan=true` — re-reads every file rather than only changed
        folders. Slow; only worth it when tags were rewritten in place."""
        before = self.scan_status().get("lastScan")
        self._ok(self._call("startScan", fullScan="true"), "startScan")
        if not wait:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.scan_status()
            if not status.get("scanning") and status.get("lastScan") != before:
                return True
            time.sleep(1.0)
        return False

    # -- library -----------------------------------------------------------

    def search_songs(self, query: str, limit: int = 50) -> list[dict]:
        body = self._ok(
            self._call("search3", query=query, songCount=limit, artistCount=0, albumCount=0),
            "search3",
            strict=False,
        )
        return body.get("searchResult3", {}).get("song", [])

    def find_song(self, title: str, artist: str) -> dict | None:
        """S11/S13: is the track in the library, and with what metadata?

        Two queries because Navidrome's full-text search is not reliably
        symmetric: a title alone can be drowned out on a large library,
        while "artist title" misses when the artist tag disagrees with what
        was asked for — which is itself one of the defects under test.
        """
        wanted_title = text_key(title)
        for query in (title, f"{artist} {title}"):
            for song in self.search_songs(query):
                if text_key(song.get("title", "")) != wanted_title:
                    continue
                credit = (
                    song.get("artist")
                    or song.get("displayArtist")
                    or song.get("albumArtist")
                    or ""
                )
                if artist_matches(credit, artist):
                    return song
        return None

    def wait_for_song(
        self, title: str, artist: str, timeout: float = 120.0, interval: float = 5.0
    ) -> dict | None:
        """Poll until the track shows up, or give up.

        Necessary because Navidrome's watcher-driven scan lands seconds
        after `trigger_scan` reports completion — see the module docstring.
        """
        deadline = time.monotonic() + timeout
        while True:
            song = self.find_song(title, artist)
            if song is not None:
                return song
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def albums(self, limit: int = 500) -> list[dict]:
        """Every album, paginated — `getAlbumList2` caps `size` at 500."""
        found: list[dict] = []
        offset = 0
        while True:
            body = self._ok(
                self._call(
                    "getAlbumList2",
                    type="alphabeticalByName",
                    size=limit,
                    offset=offset,
                ),
                "getAlbumList2",
                strict=False,
            )
            page = body.get("albumList2", {}).get("album", [])
            found.extend(page)
            if len(page) < limit:
                return found
            offset += limit

    def song_count(self) -> int:
        """Total songs in the library.

        Summed from album song counts rather than read off `getScanStatus`:
        that endpoint's `count` is the number of items touched by the *last
        scan* (0 for a no-op quick scan), not the library size — verified
        live against a library that had four tracks and reported `count: 0`.
        """
        return sum(int(album.get("songCount") or 0) for album in self.albums())

    # -- playlists ---------------------------------------------------------

    def list_playlists(self) -> list[dict]:
        body = self._ok(self._call("getPlaylists"), "getPlaylists", strict=False)
        return body.get("playlists", {}).get("playlist", [])

    def find_playlist(self, name: str) -> dict | None:
        wanted = text_key(name)
        for playlist in self.list_playlists():
            if text_key(playlist.get("name", "")) == wanted:
                return playlist
        return None

    def playlist_songs(self, playlist_id: str) -> list[dict]:
        body = self._ok(
            self._call("getPlaylist", id=playlist_id), "getPlaylist", strict=False
        )
        return body.get("playlist", {}).get("entry", [])

    def create_playlist(self, name: str) -> str:
        body = self._ok(self._call("createPlaylist", name=name), "createPlaylist")
        created = body.get("playlist", {})
        playlist_id = created.get("id")
        if playlist_id:
            return str(playlist_id)
        # Some Subsonic servers answer createPlaylist with a bare ok and no
        # body; look the playlist up by name rather than failing.
        found = self.find_playlist(name)
        if found is None:
            raise NavidromeProbeError(f"createPlaylist({name!r}) returned no playlist")
        return str(found["id"])

    def add_songs(self, playlist_id: str, song_ids: list[str]) -> bool:
        """`updatePlaylist` with `songIdToAdd` — note the parameter name;
        `songId` is silently ignored, which is how playlists end up empty."""
        if not song_ids:
            return True
        body = self._call(
            "updatePlaylist", playlistId=playlist_id, songIdToAdd=song_ids
        )
        return body.get("status") == "ok"

    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        body = self._call("updatePlaylist", playlistId=playlist_id, name=name)
        return body.get("status") == "ok"

    def delete_playlist(self, playlist_id: str) -> bool:
        """musica itself has no delete, which is part of why the playlist
        lifecycle has never been tested end to end (U9)."""
        body = self._call("deletePlaylist", id=playlist_id)
        return body.get("status") == "ok"
