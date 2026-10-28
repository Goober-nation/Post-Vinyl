"""Thin ListenBrainz client (#83).

Checked against the ListenBrainz server source (``master``) and live
(2026-10-28):
- Recording metadata needs no token; POST takes up to 1000 MBIDs. Unknown
  MBIDs are simply absent (HTTP 200). ``length`` is milliseconds. ``release``
  is ListenBrainz's main release for the recording.
- The name -> MBID lookup needs a user token (``Authorization: Token …``),
  answers 200 with or without a match, and caps the names at 250 characters.
- ``validate-token`` answers HTTP 200 with ``valid: false`` for a bad token.
- Rate limit is per window, reported in ``X-RateLimit-Remaining`` and
  ``X-RateLimit-Reset-In`` (seconds); 429 when exceeded.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import requests

USER_AGENT = "PostVinyl/0.1 ( https://github.com/Goober-nation/Post-Vinyl )"
_MAX_MBIDS_PER_POST = 1000
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
        return resp.json()

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
