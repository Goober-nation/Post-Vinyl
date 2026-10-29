"""MusicBrainz client implementing CatalogSource (#81).

Checked against MusicBrainz's API docs and live calls (2026-10-28):
- Rate limit is 1 request/s on average **per IP**; above it every request
  gets a 503 until the rate drops, so the limiter is shared by all instances
  and a 503 backs off instead of retrying at once. No Retry-After is sent.
- The User-Agent must be ``Name/version ( contact )``; the contact is this
  project's repo (legacy used an unrelated URL).
- ``length`` is milliseconds. Search results carry no ISRCs; lookups with
  ``inc=isrcs`` do. A malformed MBID is HTTP 400, an unknown one 404.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    ReleaseRef,
    Track,
)

USER_AGENT = "PostVinyl/0.1 ( https://github.com/Goober-nation/Post-Vinyl )"
_MIN_INTERVAL_S = 1.0
_BACKOFFS_S = (2.0, 5.0)  # waits after a 503 before each retry
_PAGE = 100  # MusicBrainz's maximum browse limit

_rate_lock = threading.Lock()
_last_call = 0.0


def _wait_turn() -> None:
    global _last_call
    with _rate_lock:
        wait = _last_call + _MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _phrase(value: str) -> str:
    """A Lucene phrase: inside quotes only backslash and quote are special."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parts(credits: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((c.get("name", ""), c.get("joinphrase", "")) for c in credits)


def _credit(credits: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    line = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits)
    return line, tuple(c["artist"]["id"] for c in credits if c.get("artist"))


def _seconds(length_ms: Any) -> float | None:
    return length_ms / 1000 if isinstance(length_ms, int) else None


def parse_recording(raw: dict[str, Any], **extra: Any) -> Track:
    artist, artist_ids = _credit(raw.get("artist-credit") or [])
    return Track(
        source="musicbrainz",
        id=raw["id"],
        title=raw.get("title", ""),
        artist=artist,
        artist_ids=artist_ids,
        duration_s=_seconds(raw.get("length")),
        isrcs=tuple(raw.get("isrcs") or ()),
        disambiguation=raw.get("disambiguation") or "",
        credits=_parts(raw.get("artist-credit") or []),
        video=bool(raw.get("video")),
        **extra,
    )


def parse_search_hit(raw: dict[str, Any]) -> Track:
    """A recording from /recording search: also its score and releases."""
    return parse_recording(
        raw,
        score=raw.get("score") if isinstance(raw.get("score"), int) else None,
        first_release_date=raw.get("first-release-date") or "",
        releases=tuple(
            ReleaseRef(
                status=rel.get("status") or "",
                primary_type=(rel.get("release-group") or {}).get("primary-type") or "",
                secondary_types=tuple(
                    (rel.get("release-group") or {}).get("secondary-types") or ()
                ),
            )
            for rel in raw.get("releases") or []
        ),
    )


def parse_release_group(raw: dict[str, Any]) -> Album:
    return Album(
        source="musicbrainz",
        id=raw["id"],
        title=raw.get("title", ""),
        artist=_credit(raw.get("artist-credit") or [])[0],
        primary_type=raw.get("primary-type") or "",
        secondary_types=tuple(raw.get("secondary-types") or ()),
        first_release_date=raw.get("first-release-date") or "",
    )


def parse_release(raw: dict[str, Any]) -> Edition:
    media = raw.get("media") or []
    return Edition(
        source="musicbrainz",
        id=raw["id"],
        title=raw.get("title", ""),
        date=raw.get("date") or "",
        country=raw.get("country") or "",
        status=raw.get("status") or "",
        formats=tuple(m.get("format") or "" for m in media),
        track_count=sum(m.get("track-count", 0) for m in media),
    )


# ------------------------------------------------------------ for tagging (#104)
# Shapes checked live 2026-10-29 (Nujabes – Feather, release Modal Soul):
# /recording/{id}?inc=releases+release-groups+artist-credits+isrcs lists every
# release with its status, date and release group; /release/{id}?inc=recordings+
# artist-credits+release-groups+media+isrcs has every medium with its tracks (track
# id, number, position, recording id) and ``cover-art-archive.front``.


@dataclass(frozen=True)
class RecordingRelease:
    id: str
    title: str
    date: str  # "YYYY[-MM[-DD]]" or ""
    status: str  # "Official", "Bootleg", … or ""
    country: str
    group_id: str
    primary_type: str  # "Album", "EP", "Single", …
    secondary_types: tuple[str, ...]  # "Compilation", "Live", …


@dataclass(frozen=True)
class RecordingInfo:
    id: str
    title: str
    credits: tuple[tuple[str, str], ...]  # (name, join phrase)
    artist_ids: tuple[str, ...]
    length_s: float | None
    isrcs: tuple[str, ...]
    releases: tuple[RecordingRelease, ...]


@dataclass(frozen=True)
class ReleaseTrack:
    id: str  # the release track's own MBID
    recording_id: str
    number: str  # as printed, e.g. "4" or "A1"
    position: int
    disc: int  # medium position


@dataclass(frozen=True)
class ReleaseInfo:
    id: str
    title: str
    date: str
    group_id: str
    group_first_date: str
    artist: str  # album artist credit line
    artist_ids: tuple[str, ...]
    tracks_per_disc: dict[int, int]
    tracks: tuple[ReleaseTrack, ...]
    has_front_cover: bool


class MusicBrainzTagSourceBase(ABC):
    @abstractmethod
    def recording_info(self, recording_id: str) -> RecordingInfo: ...

    @abstractmethod
    def release_info(self, release_id: str) -> ReleaseInfo: ...


def parse_recording_info(raw: dict[str, Any]) -> RecordingInfo:
    _, artist_ids = _credit(raw.get("artist-credit") or [])
    return RecordingInfo(
        id=raw["id"],
        title=raw.get("title", ""),
        credits=_parts(raw.get("artist-credit") or []),
        artist_ids=artist_ids,
        length_s=_seconds(raw.get("length")),
        isrcs=tuple(raw.get("isrcs") or ()),
        releases=tuple(
            RecordingRelease(
                id=r["id"],
                title=r.get("title", ""),
                date=r.get("date") or "",
                status=r.get("status") or "",
                country=r.get("country") or "",
                group_id=(r.get("release-group") or {}).get("id", ""),
                primary_type=(r.get("release-group") or {}).get("primary-type") or "",
                secondary_types=tuple(
                    (r.get("release-group") or {}).get("secondary-types") or ()
                ),
            )
            for r in raw.get("releases") or []
        ),
    )


def parse_release_info(raw: dict[str, Any]) -> ReleaseInfo:
    artist, artist_ids = _credit(raw.get("artist-credit") or [])
    media = raw.get("media") or []
    group = raw.get("release-group") or {}
    return ReleaseInfo(
        id=raw["id"],
        title=raw.get("title", ""),
        date=raw.get("date") or "",
        group_id=group.get("id", ""),
        group_first_date=group.get("first-release-date") or "",
        artist=artist,
        artist_ids=artist_ids,
        tracks_per_disc={m["position"]: m.get("track-count", 0) for m in media},
        tracks=tuple(
            ReleaseTrack(
                id=t["id"],
                recording_id=(t.get("recording") or {}).get("id", ""),
                number=t.get("number") or "",
                position=t.get("position") or 0,
                disc=m["position"],
            )
            for m in media
            for t in m.get("tracks") or []
        ),
        has_front_cover=bool((raw.get("cover-art-archive") or {}).get("front")),
    )


class MusicBrainzClient(CatalogSource, MusicBrainzTagSourceBase):
    source = "musicbrainz"

    def __init__(
        self, base_url: str = "https://musicbrainz.org", timeout_s: float = 15
    ) -> None:
        self._base = base_url.rstrip("/") + "/ws/2"
        self._timeout = timeout_s
        self._http = requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self._http.headers["Accept"] = "application/json"

    def _get(self, path: str, **params: Any) -> Any:
        params["fmt"] = "json"
        for backoff in (*_BACKOFFS_S, None):
            _wait_turn()
            try:
                resp = self._http.get(
                    self._base + path, params=params, timeout=self._timeout
                )
            except requests.RequestException as e:
                raise CatalogError(f"GET {path}: {e}") from e
            if resp.status_code != 503:
                break
            if backoff is None:
                raise CatalogBusy(f"GET {path}: HTTP 503 after retries")
            time.sleep(backoff)
        if resp.status_code == 404:
            raise CatalogNotFound(f"GET {path}: HTTP 404")
        if not resp.ok:
            raise CatalogError(f"GET {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _browse(self, path: str, key: str, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            page = self._get(path, limit=_PAGE, offset=len(rows), **params)
            rows += page[key]
            if not page[key] or len(rows) >= page[f"{key[:-1]}-count"]:
                return rows

    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        query = f"recording:{_phrase(title)}"
        if artist:
            query += f" AND artist:{_phrase(artist)}"
        data = self._get("/recording", query=query, limit=limit)
        return [parse_search_hit(r) for r in data["recordings"]]

    def search_artists(self, name: str, limit: int = 25) -> list[Artist]:
        data = self._get("/artist", query=f"artist:{_phrase(name)}", limit=limit)
        return [
            Artist(
                source="musicbrainz",
                id=a["id"],
                name=a.get("name", ""),
                disambiguation=a.get("disambiguation") or "",
            )
            for a in data["artists"]
        ]

    def search_albums(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Album]:
        query = f"releasegroup:{_phrase(title)}"
        if artist:
            query += f" AND artist:{_phrase(artist)}"
        data = self._get("/release-group", query=query, limit=limit)
        return [parse_release_group(g) for g in data["release-groups"]]

    def get_track(self, track_id: str) -> Track:
        data = self._get(f"/recording/{track_id}", inc="artist-credits+isrcs")
        return parse_recording(data)

    def recording_info(self, recording_id: str) -> RecordingInfo:
        inc = "releases+release-groups+artist-credits+isrcs"
        return parse_recording_info(self._get(f"/recording/{recording_id}", inc=inc))

    def release_info(self, release_id: str) -> ReleaseInfo:
        inc = "recordings+artist-credits+release-groups+media+isrcs"
        return parse_release_info(self._get(f"/release/{release_id}", inc=inc))

    def tracks_by_isrc(self, isrc: str) -> list[Track]:
        data = self._get(f"/isrc/{isrc}", inc="artist-credits+isrcs")
        return [parse_recording(r) for r in data["recordings"]]

    def artist_albums(self, artist_id: str) -> list[Album]:
        rows = self._browse(
            "/release-group",
            "release-groups",
            artist=artist_id,
            type="album|ep|single",
            inc="artist-credits",
        )
        return [parse_release_group(g) for g in rows]

    def album_editions(self, album_id: str) -> list[Edition]:
        rows = self._browse(
            "/release", "releases", **{"release-group": album_id, "inc": "media"}
        )
        return [parse_release(r) for r in rows]

    def edition_tracks(self, edition_id: str) -> list[Track]:
        data = self._get(
            f"/release/{edition_id}", inc="recordings+artist-credits+isrcs"
        )
        return [
            parse_recording(
                t["recording"], disc=m.get("position"), position=t.get("position")
            )
            for m in data.get("media") or []
            for t in m.get("tracks") or []
        ]
