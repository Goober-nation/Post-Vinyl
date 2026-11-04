"""Thin ListenBrainz client (#83).

Checked against the ListenBrainz server source (``master``) and live
(2026-10-28):
- Recording metadata needs no token; POST takes up to 1000 MBIDs. Unknown
  MBIDs are simply absent (HTTP 200). ``length`` is milliseconds. ``release``
  is ListenBrainz's main release for the recording.
- The name -> MBID lookup needs a user token (``Authorization: Token …``),
  answers 200 with or without a match, and caps the names at 250 characters.
- ``validate-token`` answers HTTP 200 with ``valid: false`` for a bad token.
- Recommendations need no token and return at most 1000 recordings, no names;
  204 = none generated yet. ``last_updated`` marks a generation. LB Radio needs
  a user token and answers JSPF (live and server source, 2026-11-04).
- Rate limit is per window, reported in ``X-RateLimit-Remaining`` and
  ``X-RateLimit-Reset-In`` (seconds); 429 when exceeded.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

USER_AGENT = "PostVinyl/0.1 ( https://github.com/Goober-nation/Post-Vinyl )"
_MAX_MBIDS_PER_POST = 1000
_MAX_RECOMMENDATIONS = 1000
_LABS_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
_SIMILAR_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100"
    "_filter_True_skip_30"
)
_JSPF_TRACK = "https://musicbrainz.org/doc/jspf#track"
_MAX_LOOKUP_CHARS = 250
_INC = "artist release tag"
_RETRIES_ON_429 = 2


class ListenBrainzError(Exception):
    """A ListenBrainz call failed: connection error, bad input or unexpected status."""


class ListenBrainzAuthError(ListenBrainzError):
    """Missing or invalid user token (HTTP 401)."""


class ListenBrainzBusy(ListenBrainzError):
    """Still rate-limited after waiting out the window (HTTP 429)."""


@dataclass(frozen=True)
class Release:
    name: str
    mbid: str
    release_group_mbid: str
    year: int | None


@dataclass(frozen=True)
class RecordingMetadata:
    mbid: str
    title: str
    length_s: float | None
    artist: str  # credit line with join phrases, e.g. "A feat. B"
    artist_mbids: tuple[str, ...]
    release: Release | None  # ListenBrainz's main release for the recording
    # genre tags, most used first
    recording_tags: tuple[str, ...]
    artist_tags: tuple[str, ...]
    release_group_tags: tuple[str, ...]


@dataclass(frozen=True)
class TokenCheck:
    valid: bool
    user_name: str | None


@dataclass(frozen=True)
class RecommendedRecording:
    mbid: str
    score: float
    latest_listened_at: str | None


@dataclass(frozen=True)
class Recommendations:
    last_updated: int  # identifies this generation of the user's list
    recordings: tuple[RecommendedRecording, ...]


@dataclass(frozen=True)
class RadioTrack:
    mbid: str
    title: str
    artist: str
    album: str | None
    length_s: float | None
    release_mbid: str | None = None


@dataclass(frozen=True)
class PopularRecording:
    mbid: str
    title: str
    artist: str
    length_s: float | None
    release_mbid: str | None


@dataclass(frozen=True)
class SimilarArtist:
    mbid: str
    name: str
    comment: str


class ListenBrainzClientBase(ABC):
    @abstractmethod
    def recording_metadata(self, mbids: list[str]) -> dict[str, RecordingMetadata]:
        """Metadata per MBID; unknown MBIDs are left out."""

    @abstractmethod
    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        """The recording ListenBrainz maps ``artist`` + ``title`` to, or None."""

    @abstractmethod
    def validate_token(self, token: str) -> TokenCheck: ...

    @abstractmethod
    def recommendations(self, user_name: str) -> Recommendations | None:
        """The user's list, best first; None until ListenBrainz has generated one."""

    @abstractmethod
    def top_recordings(self, artist_mbid: str) -> list[PopularRecording]:
        """The artist's recordings, most listened first."""

    @abstractmethod
    def similar_artists(self, artist_mbid: str) -> list[SimilarArtist]:
        """Most similar first (ListenBrainz Labs, no token)."""

    @abstractmethod
    def radio(self, artist: str, level: str, token: str) -> list[RadioTrack]:
        """A fresh LB Radio for ``artist``; level is easy, medium or hard."""


def parse_radio_track(raw: dict[str, Any]) -> RadioTrack | None:
    identifiers = raw.get("identifier") or []
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    mbid = identifiers[0].rstrip("/").rsplit("/", 1)[-1] if identifiers else ""
    if not (mbid and raw.get("title") and raw.get("creator")):
        return None
    duration = raw.get("duration")
    extension = (raw.get("extension") or {}).get(_JSPF_TRACK) or {}
    release = extension.get("release_identifier") or ""
    return RadioTrack(
        mbid=mbid,
        title=raw["title"],
        artist=raw["creator"],
        album=raw.get("album"),
        length_s=duration / 1000 if isinstance(duration, int) else None,
        release_mbid=release.rstrip("/").rsplit("/", 1)[-1] or None,
    )


def _tag_names(rows: list[dict[str, Any]] | None) -> tuple[str, ...]:
    ordered = sorted(rows or [], key=lambda r: -(r.get("count") or 0))
    return tuple(dict.fromkeys(r["tag"] for r in ordered if r.get("tag")))


def parse_metadata(mbid: str, raw: dict[str, Any]) -> RecordingMetadata:
    recording = raw.get("recording") or {}
    artists = (raw.get("artist") or {}).get("artists") or []
    release = raw.get("release")
    tags = raw.get("tag") or {}
    length = recording.get("length")
    return RecordingMetadata(
        mbid=mbid,
        title=recording.get("name", ""),
        length_s=length / 1000 if isinstance(length, int) else None,
        artist="".join(
            a.get("name", "") + (a.get("join_phrase") or "") for a in artists
        ),
        artist_mbids=tuple(a["artist_mbid"] for a in artists if a.get("artist_mbid")),
        release=Release(
            name=release.get("name", ""),
            mbid=release.get("mbid", ""),
            release_group_mbid=release.get("release_group_mbid", ""),
            year=release.get("year"),
        )
        if release
        else None,
        recording_tags=_tag_names(tags.get("recording")),
        artist_tags=_tag_names(tags.get("artist")),
        release_group_tags=_tag_names(tags.get("release_group")),
    )


class ListenBrainzClient(ListenBrainzClientBase):
    def __init__(
        self, base_url: str = "https://api.listenbrainz.org", timeout_s: float = 15
    ) -> None:
        self._base = base_url.rstrip("/") + "/1"
        self._timeout = timeout_s
        self._http = requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT

    def _call(
        self, method: str, path: str, token: str | None = None, **kwargs: Any
    ) -> Any:
        headers = {"Authorization": f"Token {token}"} if token else {}
        for attempt in range(_RETRIES_ON_429 + 1):
            try:
                resp = self._http.request(
                    method,
                    self._base + path,
                    headers=headers,
                    timeout=self._timeout,
                    **kwargs,
                )
            except requests.RequestException as e:
                raise ListenBrainzError(f"{method} {path}: {e}") from e
            reset_in = float(resp.headers.get("X-RateLimit-Reset-In") or 1)
            if resp.status_code != 429:
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(reset_in)  # the next call would be refused
                break
            if attempt == _RETRIES_ON_429:
                raise ListenBrainzBusy(f"{method} {path}: HTTP 429")
            time.sleep(reset_in)
        if resp.status_code == 401:
            raise ListenBrainzAuthError(f"{method} {path}: HTTP 401")
        if not resp.ok:
            raise ListenBrainzError(
                f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return None if resp.status_code == 204 else resp.json()

    def recording_metadata(self, mbids: list[str]) -> dict[str, RecordingMetadata]:
        result: dict[str, RecordingMetadata] = {}
        for i in range(0, len(mbids), _MAX_MBIDS_PER_POST):
            chunk = mbids[i : i + _MAX_MBIDS_PER_POST]
            body = {"recording_mbids": chunk, "inc": _INC}
            data = self._call("POST", "/metadata/recording/", json=body)
            result |= {m: parse_metadata(m, raw) for m, raw in data.items()}
        return result

    def lookup_recording(
        self, artist: str, title: str, token: str
    ) -> RecordingMetadata | None:
        if len(artist) + len(title) > _MAX_LOOKUP_CHARS:
            raise ListenBrainzError(f"lookup names over {_MAX_LOOKUP_CHARS} characters")
        params = {
            "artist_name": artist,
            "recording_name": title,
            "metadata": "true",
            "inc": _INC,
        }
        match = self._call("GET", "/metadata/lookup/", token=token, params=params)
        if not match or not match.get("recording_mbid"):
            return None
        mbid = match["recording_mbid"]
        meta = parse_metadata(mbid, match.get("metadata") or {})
        if meta.title:
            return meta
        # no metadata block: fall back to the match's own fields
        return RecordingMetadata(
            mbid=mbid,
            title=match.get("recording_name", ""),
            length_s=None,
            artist=match.get("artist_credit_name", ""),
            artist_mbids=tuple(match.get("artist_mbids") or ()),
            release=None,
            recording_tags=(),
            artist_tags=(),
            release_group_tags=(),
        )

    def validate_token(self, token: str) -> TokenCheck:
        data = self._call("GET", "/validate-token", token=token)
        return TokenCheck(
            valid=bool(data.get("valid")), user_name=data.get("user_name")
        )

    def recommendations(self, user_name: str) -> Recommendations | None:
        path = f"/cf/recommendation/user/{quote(user_name, safe='')}/recording"
        data = self._call("GET", path, params={"count": _MAX_RECOMMENDATIONS})
        if data is None:
            return None
        payload = data["payload"]
        return Recommendations(
            last_updated=payload["last_updated"],
            recordings=tuple(
                RecommendedRecording(
                    m["recording_mbid"], m["score"], m.get("latest_listened_at")
                )
                for m in payload["mbids"]
            ),
        )

    def radio(self, artist: str, level: str, token: str) -> list[RadioTrack]:
        params = {"prompt": f"artist:({artist})", "mode": level}
        data = self._call("GET", "/explore/lb-radio", token=token, params=params)
        tracks = data["payload"]["jspf"]["playlist"]["track"]
        return [t for t in map(parse_radio_track, tracks) if t]

    def top_recordings(self, artist_mbid: str) -> list[PopularRecording]:
        rows = self._call("GET", f"/popularity/top-recordings-for-artist/{artist_mbid}")
        return [
            PopularRecording(
                r["recording_mbid"],
                r.get("recording_name") or "",
                r.get("artist_name") or "",
                r["length"] / 1000 if r.get("length") else None,
                r.get("caa_release_mbid") or r.get("release_mbid"),
            )
            for r in rows
            if r.get("recording_mbid")
        ]

    def similar_artists(self, artist_mbid: str) -> list[SimilarArtist]:
        try:
            resp = self._http.get(
                _LABS_SIMILAR,
                params={"artist_mbids": artist_mbid, "algorithm": _SIMILAR_ALGORITHM},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise ListenBrainzError(f"GET similar-artists: {e}") from e
        return [
            SimilarArtist(a["artist_mbid"], a.get("name", ""), a.get("comment") or "")
            for a in resp.json()
            if a.get("artist_mbid") != artist_mbid
        ]
