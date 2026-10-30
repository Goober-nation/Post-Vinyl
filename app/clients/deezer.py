"""Deezer client implementing CatalogSource (#82).

Checked live (2026-10-28); Deezer's docs now need a login, so nothing here
comes from them or from legacy on trust:
- Errors come as HTTP 200 with ``{"error": {"type", "message", "code"}}``;
  code 800 = no data. Codes 4 (quota) and 700 (service busy) are retried.
- The advanced-search ``artist:"…"`` field is broken (``artist:"Queen"``
  matches titles; even Deezer's own doc example returns nothing), so searches
  send title and artist as a plain query.
- ``track/isrc:<ISRC>`` returns one track although several can share it.
- Durations are whole seconds. An album has one tracklist, so it has exactly
  one edition, with the album's id.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from app.clients.catalog import (
    Album,
    Artist,
    CatalogBusy,
    CatalogError,
    CatalogNotFound,
    CatalogSource,
    Edition,
    Track,
)

# Commonly cited quota: 50 requests per 5 s; stay at or under it.
_MIN_INTERVAL_S = 0.1
_BACKOFFS_S = (2.0, 5.0)
_PAGE = 100
_NO_DATA = 800
_RETRY_CODES = {4, 700}
# record_type -> (primary type, secondary types), in MusicBrainz's terms
_RECORD_TYPES = {
    "album": ("Album", ()),
    "ep": ("EP", ()),
    "single": ("Single", ()),
    "compile": ("Album", ("Compilation",)),
}

_MD5 = re.compile(r"[0-9a-f]{32}")
_rate_lock = threading.Lock()
_last_call = 0.0


def _wait_turn() -> None:
    global _last_call
    with _rate_lock:
        wait = _last_call + _MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def parse_track(raw: dict[str, Any]) -> Track:
    contributors = raw.get("contributors") or [raw.get("artist") or {}]
    return Track(
        source="deezer",
        id=str(raw["id"]),
        title=raw.get("title_short") or raw.get("title", ""),
        artist=(raw.get("artist") or {}).get("name", ""),
        artist_ids=tuple(str(c["id"]) for c in contributors if "id" in c),
        duration_s=float(raw["duration"]) if raw.get("duration") else None,
        isrcs=(raw["isrc"],) if raw.get("isrc") else (),
        disambiguation=raw.get("title_version") or "",
        # `artist` is the main artist only (featured ones are in contributors)
        credits=(((raw.get("artist") or {}).get("name", ""), ""),),
        disc=raw.get("disk_number"),
        position=raw.get("track_position"),
        # its album's image id: on the track in tracklists, on `album` in searches
        cover_id=raw.get("md5_image") or (raw.get("album") or {}).get("md5_image", ""),
    )


def parse_album(raw: dict[str, Any], artist: str = "") -> Album:
    primary, secondary = _RECORD_TYPES.get(raw.get("record_type", ""), ("", ()))
    return Album(
        source="deezer",
        id=str(raw["id"]),
        title=raw.get("title", ""),
        artist=(raw.get("artist") or {}).get("name", artist),
        primary_type=primary,
        secondary_types=secondary,
        first_release_date=raw.get("release_date") or "",
        cover_id=raw.get("md5_image") or "",
    )


class DeezerClient(CatalogSource):
    source = "deezer"

    def __init__(
        self,
        base_url: str = "https://api.deezer.com",
        timeout_s: float = 15,
        images_url: str = "https://cdn-images.dzcdn.net",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._images = images_url.rstrip("/")
        self._timeout = timeout_s
        self._http = requests.Session()

    def _get(self, path: str, **params: Any) -> Any:
        for backoff in (*_BACKOFFS_S, None):
            _wait_turn()
            try:
                resp = self._http.get(
                    self._base + path, params=params, timeout=self._timeout
                )
            except requests.RequestException as e:
                raise CatalogError(f"GET {path}: {e}") from e
            if not resp.ok:
                raise CatalogError(f"GET {path}: HTTP {resp.status_code}")
            body = resp.json()
            error = body.get("error") if isinstance(body, dict) else None
            if not error:
                return body
            msg = f"GET {path}: Deezer error {error.get('code')} {error.get('message')}"
            if error.get("code") == _NO_DATA:
                raise CatalogNotFound(msg)
            if error.get("code") not in _RETRY_CODES:
                raise CatalogError(msg)
            if backoff is None:
                raise CatalogBusy(msg)
            time.sleep(backoff)
        raise AssertionError("unreachable")

    def _paged(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            page = self._get(path, index=len(rows), limit=_PAGE)
            rows += page.get("data") or []
            if not page.get("data") or "next" not in page:
                return rows

    def _search(self, kind: str, query: str, limit: int) -> list[dict[str, Any]]:
        return self._get(f"/search/{kind}", q=query, limit=limit).get("data") or []

    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        query = f"{title} {artist}" if artist else title
        return [parse_track(t) for t in self._search("track", query, limit)]

    def search_artists(self, name: str, limit: int = 25) -> list[Artist]:
        return [
            Artist(
                source="deezer",
                id=str(a["id"]),
                name=a.get("name", ""),
                disambiguation="",
            )
            for a in self._search("artist", name, limit)
        ]

    def search_albums(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Album]:
        query = f"{title} {artist}" if artist else title
        return [parse_album(a) for a in self._search("album", query, limit)]

    def get_track(self, track_id: str) -> Track:
        return parse_track(self._get(f"/track/{track_id}"))

    def tracks_by_isrc(self, isrc: str) -> list[Track]:
        return [parse_track(self._get(f"/track/isrc:{isrc}"))]

    def artist_albums(self, artist_id: str) -> list[Album]:
        name = self._get(f"/artist/{artist_id}").get("name", "")
        return [
            parse_album(a, name) for a in self._paged(f"/artist/{artist_id}/albums")
        ]

    def album_editions(self, album_id: str) -> list[Edition]:
        raw = self._get(f"/album/{album_id}")
        return [
            Edition(
                source="deezer",
                id=str(raw["id"]),
                title=raw.get("title", ""),
                date=raw.get("release_date") or "",
                country="",
                status="",
                formats=(),
                track_count=raw.get("nb_tracks", 0),
                cover_id=raw.get("md5_image") or "",
            )
        ]

    def cover(self, cover_id: str, size: int) -> bytes | None:
        """An album image from Deezer's image server by its ``md5_image`` id,
        ``size`` px square (#127). Checked live 2026-10-31: any square size is
        served; an unknown id redirects to a placeholder, taken as none."""
        if not _MD5.fullmatch(cover_id):
            return None
        url = f"{self._images}/images/cover/{cover_id}/{size}x{size}-000000-80-0-0.jpg"
        try:
            resp = self._http.get(url, timeout=self._timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise CatalogError(f"GET {url}: {e}") from e
        if resp.status_code != 200 or not resp.headers.get(
            "Content-Type", ""
        ).startswith("image/"):
            return None
        return resp.content

    def edition_tracks(self, edition_id: str) -> list[Track]:
        return [parse_track(t) for t in self._paged(f"/album/{edition_id}/tracks")]

    def chart_tracks(self, limit: int = 100) -> list[Track]:
        """Deezer's global top tracks."""
        data = self._get("/chart/0/tracks", limit=limit).get("data") or []
        return [parse_track(t) for t in data]
