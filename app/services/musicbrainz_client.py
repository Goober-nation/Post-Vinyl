"""
MusicBrainzClient — the real HTTP implementation of `MusicBrainzService`.

P-MB-1. Built ahead of the rest of Phase 6.8 because the import path needs
canonical metadata *now*: without it beets matches a downloaded file against
whatever identity the file claims, which the 2026-08-12 live run showed
producing `Various Artists / LateNightTales` for Björk's "Jóga" and the live
`I Might Be Wrong` version of a Radiohead album track.

Three things about MusicBrainz that are not optional:

1. **A descriptive User-Agent is required.** Requests without one are
   answered with 403. It must identify the application and provide a contact
   URL — Musica uses its public project URL automatically.
2. **One request per second, averaged.** Exceeding it earns a 503, and
   sustained abuse earns a block. `_RateLimiter` enforces this process-wide
   and across threads, because the rec puller and the download monitor both
   run in their own threads and neither knows about the other.
3. **A search is a Lucene query.** Unescaped punctuation in a track title
   silently changes the query's meaning — `ALICE_`, `Jóga`, and
   `Write This Down (feat. Nieve)` all contain characters Lucene treats as
   syntax. `escape_lucene` handles it; it has its own unit tests because a
   silently-mangled query returns plausible-looking wrong answers rather
   than an error.

Everything returns the dataclasses from `app.services.interfaces.musicbrainz`,
never raw JSON, so nothing downstream grows a dependency on MusicBrainz's
response shape.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from app.exceptions import MusicBrainzConnectionError, MusicBrainzRateLimitError
from app.logging_config import get_logger
from app.services.interfaces.musicbrainz import (
    MBArtist,
    MBRecording,
    MBRelease,
    MBReleaseGroup,
    MusicBrainzService,
)

logger = get_logger(__name__)

DEFAULT_MUSICBRAINZ_URL = "https://musicbrainz.org"

#: MusicBrainz asks for an identifying application/version and a contact URL.
#: The project URL is stable and avoids putting a personal address in config.
MUSICBRAINZ_UA_TEMPLATE = "musica/{version} ( {contact} )"
MUSICBRAINZ_CONTACT_URL = "https://github.com/musica"

#: Characters Lucene treats as query syntax. Escaped, not stripped: the
#: title `ALICE_` really does end in an underscore and the search should say
#: so.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

#: Query clauses that bias a recording search toward canonical studio
#: releases.
#:
#: **Positive only — no `-secondarytype:X` negations.** That was the first
#: version of this filter, and it was wrong in a way worth recording:
#: MusicBrainz indexes `secondarytype` (and `status`) **per recording**,
#: pooled across every release that recording appears on, not per release.
#: Madvillain's "All Caps" legitimately appears on the studio album
#: (`Madvillainy`, no secondary type) *and* on a remix album *and* on a
#: compilation. Adding `-secondarytype:remix` or `-secondarytype:compilation`
#: didn't exclude those unwanted releases — it excluded the **entire
#: recording**, `Madvillainy` release included, because the recording's
#: pooled secondarytype set contains "remix" and "compilation" regardless of
#: which specific release is being asked about. Verified directly against
#: the live API on 2026-08-12: dropping just the `-secondarytype:compilation`
#: clause was enough to make the correct recording reappear.
#:
#: `status:official AND primarytype:album` is safe because it is positive:
#: it requires the recording to have *at least one* qualifying release,
#: without punishing it for also having others. The actual studio-vs-not
#: discrimination happens client-side in `MBRecording.best_release`, which
#: already has each candidate's full release list to work with.
#: Positive "has at least one official release" clause. Safe where the
#: pooled per-recording fields are safe (it requires a qualifying release
#: without punishing the recording for also having a bootleg one) — see the
#: `_CANONICAL_CLAUSES` docstring for why negations were avoided.
_OFFICIAL_STATUS_CLAUSE = "status:official"

_CANONICAL_CLAUSES = (
    _OFFICIAL_STATUS_CLAUSE,
    "primarytype:album",
)
#: Words in a release title that mark it as an alternate edition rather than
#: the album someone meant. A heuristic, and labelled as one — MusicBrainz
#: has no machine-readable "this is the instrumental edition" flag, and the
#: `disambiguation` field is empty on every case checked. Used only to break
#: ties *after* the query filter has done the real work, so a wrong guess
#: here costs a slightly-off album name, never a bootleg.
VARIANT_MARKERS: tuple[str, ...] = (
    "instrumental",
    "instrumentals",
    "karaoke",
    "acappella",
    "a cappella",
    "acoustic",
    "commentary",
    "demos",
    "preview",
    "rehearsal",
    "outtakes",
    "sampler",
    # These mark an alternate *recording* rather than an alternate
    # *release* — added after live-verifying that a same-artist bootleg
    # ("Feather (N0ms Bootleg)") outscored the real studio recording on
    # MusicBrainz's own text relevance (100 vs 77 for "Feather") and won
    # by being the only candidate that cleared `min_score`. Checked
    # against recording titles too, not just release titles — see
    # `resolve_canonical`. Deliberately excludes shorter/ambiguous words
    # like "edit" or "cover" that show up as false-positive substrings
    # ("Edition", "Undercover") or legitimately name the canonical single
    # release ("Radio Edit").
    "bootleg",
    "remix",
    "mashup",
    "rework",
    "tribute",
)


def looks_like_variant(title: str) -> bool:
    """True when a release title advertises itself as an alternate edition."""
    lowered = title.lower()
    return any(marker in lowered for marker in VARIANT_MARKERS)


#: How far below `min_score` a candidate may fall and still be considered
#: for the near-miss studio fallback in `resolve_canonical`. Chosen from the
#: one live-verified case: the real Nujabes - Feather recording scored 77
#: against a min_score of 90 (a 13-point gap), penalized for a longer,
#: fully-credited artist string ("Nujabes featuring Cise Starr & Akin from
#: CYNE") relative to a bootleg's bare "Nujabes". 15 covers that case with a
#: little headroom without opening the gate very wide — this only ever
#: activates when nothing at or above min_score has a canonical studio
#: release at all, so a bad guess here replaces an already-bad guess
#: (a same-titled bootleg/live/compilation-only match), never a good one.
NEAR_MISS_STUDIO_SCORE_MARGIN: int = 15


def escape_lucene(value: str) -> str:
    """Escape a user-supplied string for use inside a Lucene query.

    `&&` and `||` are handled by escaping each character individually, which
    is what MusicBrainz's own examples do.
    """
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


class _RateLimiter:
    """Process-wide minimum interval between requests.

    Threading matters here: `RecPuller` and `DownloadMonitor` are separate
    threads that will both want lookups, and MusicBrainz's limit is per
    client, not per thread. A lock held across the sleep is what makes the
    *average* rate correct rather than merely the per-thread rate.
    """

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> float:
        """Block until it is safe to issue a request. Returns seconds slept."""
        with self._lock:
            now = time.monotonic()
            gap = now - self._last
            slept = 0.0
            if gap < self.min_interval:
                slept = self.min_interval - gap
                time.sleep(slept)
            self._last = time.monotonic()
            return slept


class _TTLCache:
    """Small bounded cache with expiry.

    Lookups repeat constantly — every track off one album resolves the same
    artist — and each miss costs a full second of rate limit. Bounded so a
    long-running process cannot grow it without limit.
    """

    def __init__(self, ttl: float = 3600.0, max_entries: int = 512) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at > self.ttl:
                del self._data[key]
                return None
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _artist_credit_display(credits: list[dict]) -> str:
    """Rebuild the full display credit, joinphrases included.

    MusicBrainz models "Alesso feat. Tove Lo" as two credits with a
    `joinphrase` of " feat. " on the first. Reassembling it is how the
    display name is produced *without* it becoming the album artist.
    """
    out = []
    for credit in credits or []:
        out.append(credit.get("name") or credit.get("artist", {}).get("name", ""))
        out.append(credit.get("joinphrase", ""))
    return "".join(out).strip()


def _primary_artist(credits: list[dict]) -> tuple[str, str | None]:
    """The first credited artist alone — no featuring clause.

    This is what a folder gets named after. The live run found featured
    artists promoted into albumartist, which is how one artist ended up
    spread across `Tyler, The Creator`, `Tyler, the Creator` and
    `Tyler, The Creator ft. Rex Orange County`.
    """
    if not credits:
        return "", None
    first = credits[0]
    artist = first.get("artist") or {}
    name = artist.get("name") or first.get("name") or ""
    return name, artist.get("id")


def _parse_release(data: dict, group: dict | None = None) -> MBRelease:
    # `group` is an override for the release's parent release-group. It exists
    # because a release-group *lookup* (`inc=releases`) returns its child
    # releases *without* a nested `release-group` object — that would be the
    # parent pointing at itself — so the group's type has to be threaded in
    # from the top-level lookup response instead. A release-group *search* or
    # *browse*, by contrast, does embed `release-group`, and those callers
    # pass nothing.
    group = group if group is not None else (data.get("release-group") or {})
    media = data.get("media") or []
    track_number: int | None = None
    track_count: int | None = None
    if media:
        track_count = media[0].get("track-count")
        tracks = media[0].get("track") or media[0].get("tracks") or []
        if tracks:
            raw = tracks[0].get("number")
            if raw is not None and str(raw).isdigit():
                track_number = int(raw)
    return MBRelease(
        mbid=data.get("id", ""),
        title=data.get("title", ""),
        primary_type=group.get("primary-type") or data.get("primary-type"),
        secondary_types=list(
            group.get("secondary-types") or data.get("secondary-types") or []
        ),
        date=data.get("date"),
        country=data.get("country"),
        artist_credit=_artist_credit_display(data.get("artist-credit") or []) or None,
        track_number=track_number,
        track_count=track_count,
        status=data.get("status"),
        release_group_mbid=group.get("id"),
    )


def _parse_release_group(data: dict) -> MBReleaseGroup:
    """Build an `MBReleaseGroup` from a release-group search/browse hit.

    The year comes from `first-release-date`, the group's own canonical date,
    not from any individual release's `date`.
    """
    credits = data.get("artist-credit") or []
    artist, artist_mbid = _primary_artist(credits)
    date = data.get("first-release-date")
    year: int | None = None
    if date:
        head = str(date).split("-", 1)[0]
        year = int(head) if head.isdigit() else None
    return MBReleaseGroup(
        mbid=data.get("id", ""),
        title=data.get("title", ""),
        artist=artist,
        artist_mbid=artist_mbid,
        primary_type=data.get("primary-type") or None,
        year=year,
        secondary_types=list(data.get("secondary-types") or []),
        release_count=data.get("count"),
        score=data.get("score"),
    )


def _parse_recording(data: dict) -> MBRecording:
    credits = data.get("artist-credit") or []
    artist, artist_mbid = _primary_artist(credits)
    return MBRecording(
        mbid=data.get("id", ""),
        title=data.get("title", ""),
        artist_credit=_artist_credit_display(credits),
        artist=artist,
        artist_mbid=artist_mbid,
        length_ms=data.get("length"),
        disambiguation=data.get("disambiguation") or None,
        score=data.get("score"),
        releases=[_parse_release(r) for r in (data.get("releases") or [])],
    )


def _parse_artist(data: dict) -> MBArtist:
    return MBArtist(
        mbid=data.get("id", ""),
        name=data.get("name", ""),
        sort_name=data.get("sort-name"),
        disambiguation=data.get("disambiguation") or None,
        score=data.get("score"),
    )


def _select_canonical_release(group_data: dict) -> MBRelease | None:
    """Pick the release of a release-group lookup whose tracks to return.

    All of a group's releases share the group's type (there is no per-release
    secondary-type), so the selector first requires an official release, then
    uses `MBRelease.is_official` to exclude bootleg/promotional pressings.
    Earliest-by-date is the anti-reissue rule: the original album, not a later
    remaster or box set. A non-canonical group still falls back to its earliest
    release so the album-to-track endpoint does not return nothing.
    """
    primary_type = group_data.get("primary-type") or None
    secondary_types = list(group_data.get("secondary-types") or [])
    group = {"primary-type": primary_type, "secondary-types": secondary_types}
    releases = [_parse_release(r, group) for r in (group_data.get("releases") or [])]
    if not releases:
        return None
    official = [r for r in releases if r.is_official]
    pool = official or releases
    return min(pool, key=lambda r: (r.year is None, r.year or 0, r.title))


def _release_tracks(release_data: dict) -> list[MBRecording]:
    """Walk a release's media/tracks in order, building one `MBRecording` per
    track.

    Order is preserved from the API (media position, then track position) —
    the track list is only ever useful in album order, so no re-sorting.
    """
    recordings: list[MBRecording] = []
    for medium in release_data.get("media") or []:
        tracks = medium.get("track") or medium.get("tracks") or []
        for track in tracks:
            recording = track.get("recording") or {}
            if recording.get("id"):
                recordings.append(_parse_recording(recording))
    return recordings


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MusicBrainzClient(MusicBrainzService):
    """Rate-limited, cached MusicBrainz client."""

    def __init__(self, config) -> None:
        self._config = config
        mb = getattr(config, "musicbrainz", None)
        self.base_url = (
            getattr(mb, "url", DEFAULT_MUSICBRAINZ_URL) or DEFAULT_MUSICBRAINZ_URL
        ).rstrip("/")
        self.timeout = getattr(mb, "timeout_seconds", 15)
        self.enabled = getattr(mb, "enabled", True)
        self._user_agent = MUSICBRAINZ_UA_TEMPLATE.format(
            version=getattr(mb, "version", "0.1"), contact=MUSICBRAINZ_CONTACT_URL
        )
        self._limiter = _RateLimiter(getattr(mb, "min_request_interval", 1.0))
        self._cache = _TTLCache(ttl=getattr(mb, "cache_ttl_seconds", 3600))
        self._session = requests.Session()

    # -- plumbing ----------------------------------------------------------

    @property
    def _headers(self) -> dict:
        return {"User-Agent": self._user_agent, "Accept": "application/json"}

    def _get(self, path: str, params: dict, retries: int = 2) -> dict:
        """One rate-limited GET, with retry/backoff on transient failures.

        MusicBrainz is a busy public service: both 503s ("you are going too
        fast") and connection/read timeouts are transient, so the retry
        loop backs off and re-asks for either (live 2026-08-14: a 15s read
        timeout on a "hot vodka 2" search aborted the whole search tab
        with a 503 when a single retry would have succeeded seconds later).
        After the retries are spent it raises rather than returning empty:
        an empty result would be indistinguishable from "no such recording",
        and silently treating throttling as "not found" would make the
        import path quietly fall back to the peer's tags — the exact failure
        this client exists to prevent.
        """
        url = f"{self.base_url}/ws/2/{path.lstrip('/')}"
        params = {**params, "fmt": "json"}
        attempt = 0
        while True:
            self._limiter.wait()
            try:
                resp = self._session.get(
                    url, params=params, headers=self._headers, timeout=self.timeout
                )
            except requests.exceptions.RequestException as e:
                if attempt >= retries:
                    raise MusicBrainzConnectionError(self.base_url, str(e)) from e
                backoff = 2.0 * (attempt + 1)
                logger.warning(
                    "MusicBrainz unreachable (attempt %d/%d): %s; backing off %.1fs",
                    attempt + 1,
                    retries + 1,
                    e,
                    backoff,
                )
                time.sleep(backoff)
                attempt += 1
                continue

            if resp.status_code == 503:
                if attempt >= retries:
                    retry_after = resp.headers.get("Retry-After")
                    raise MusicBrainzRateLimitError(
                        float(retry_after) if retry_after else None
                    )
                backoff = 2.0 * (attempt + 1)
                logger.warning(
                    "MusicBrainz throttled us (503), backing off %.1fs", backoff
                )
                time.sleep(backoff)
                attempt += 1
                continue

            if resp.status_code == 404:
                return {}
            if resp.status_code != 200:
                raise MusicBrainzConnectionError(
                    self.base_url, f"HTTP {resp.status_code}"
                )
            try:
                return resp.json()
            except ValueError as e:
                raise MusicBrainzConnectionError(
                    self.base_url, f"malformed JSON: {e}"
                ) from e

    def _cached_get(self, cache_key: tuple, path: str, params: dict) -> dict:
        hit = self._cache.get(cache_key)
        if hit is not None:
            return hit
        data = self._get(path, params)
        self._cache.put(cache_key, data)
        return data

    # -- searches ----------------------------------------------------------

    def search_recording(
        self,
        title: str,
        artist: str | None = None,
        limit: int = 10,
        official_only: bool = False,
        *,
        canonical_only: bool = False,
    ) -> list[MBRecording]:
        """Find recordings matching a title (and optionally an artist).

        `canonical_only` appends `_CANONICAL_CLAUSES`, restricting results to
        official studio albums. The import path always wants this; a
        user-facing search (P-MB-2) generally does not, since someone
        explicitly looking for a live album should be able to find one.

        `official_only` (the search-tab default) filters the results to
        recordings with at least one official release. It filters *after* the
        API call on a buffered fetch, never as a query clause: a query-level
        `status:official` clause re-scores the results rather than just
        removing noise, and was live-verified 2026-08-13 to push the canonical
        "All Caps" (Madvillainy) recording out of the top 20 and past the
        default `limit`. The recording search response carries each
        recording's full release list, so `MBRecording.is_official` is a
        reliable client-side test here.
        """
        if not self.enabled or not title.strip():
            return []
        clauses = [f'recording:"{escape_lucene(title)}"']
        if artist:
            clauses.append(f'artist:"{escape_lucene(artist)}"')
        if canonical_only:
            clauses.extend(_CANONICAL_CLAUSES)
        query = " AND ".join(clauses)
        # Official-only filters *after* the fetch, so grab a buffer and slice
        # back down — the same "filter before the count slice" rule as
        # `search_release_group` and the recs pipeline. Without the buffer, a
        # head of bootleg/compilation recordings would hide the album one row
        # further down.
        fetch_limit = limit if not official_only else min(100, max(limit * 3, 25))
        data = self._cached_get(
            ("recording", query, fetch_limit),
            "recording",
            {"query": query, "limit": fetch_limit},
        )
        recordings = [_parse_recording(r) for r in data.get("recordings", [])]
        if official_only:
            recordings = [r for r in recordings if r.is_official]
        return recordings[:limit]

    def search_release(
        self, title: str, artist: str | None = None, limit: int = 10
    ) -> list[MBRelease]:
        if not self.enabled or not title.strip():
            return []
        clauses = [f'release:"{escape_lucene(title)}"']
        if artist:
            clauses.append(f'artist:"{escape_lucene(artist)}"')
        query = " AND ".join(clauses)
        data = self._cached_get(
            ("release", query, limit), "release", {"query": query, "limit": limit}
        )
        return [_parse_release(r) for r in data.get("releases", [])]

    def search_release_group(
        self,
        title: str,
        artist: str | None = None,
        limit: int = 10,
        official_only: bool = False,
    ) -> list[MBReleaseGroup]:
        if not self.enabled or not title.strip():
            return []
        clauses = [f'releasegroup:"{escape_lucene(title)}"']
        if artist:
            clauses.append(f'artist:"{escape_lucene(artist)}"')
        query = " AND ".join(clauses)
        # Official-only filters *after* the API call, so fetch a buffer and
        # slice back down — otherwise a query whose top N hits are all
        # mixtapes/live albums would come back empty even though the album is
        # one row further down (the same "filter before the count slice"
        # rule as the recs pipeline).
        fetch_limit = limit if not official_only else min(100, max(limit * 3, 25))
        data = self._cached_get(
            ("release-group", query, fetch_limit),
            "release-group",
            {"query": query, "limit": fetch_limit},
        )
        groups = [_parse_release_group(g) for g in data.get("release-groups", [])]
        if official_only:
            groups = [g for g in groups if g.is_official]
        return groups[:limit]

    def search_artist(self, name: str, limit: int = 10) -> list[MBArtist]:
        if not self.enabled or not name.strip():
            return []
        query = f'artist:"{escape_lucene(name)}"'
        data = self._cached_get(
            ("artist", query, limit), "artist", {"query": query, "limit": limit}
        )
        return [_parse_artist(a) for a in data.get("artists", [])]

    def browse_artist_releases(
        self, artist_mbid: str, limit: int = 100
    ) -> list[MBRelease]:
        if not self.enabled or not artist_mbid:
            return []
        data = self._cached_get(
            ("browse-release", artist_mbid, limit),
            "release",
            {"artist": artist_mbid, "inc": "release-groups", "limit": limit},
        )
        return [_parse_release(r) for r in data.get("releases", [])]

    def browse_artist_release_groups(
        self, artist_mbid: str, limit: int = 100, official_only: bool = False
    ) -> list[MBReleaseGroup]:
        if not self.enabled or not artist_mbid:
            return []
        data = self._cached_get(
            ("browse-release-group", artist_mbid, limit),
            "release-group",
            {"artist": artist_mbid, "limit": limit},
        )
        groups = [_parse_release_group(g) for g in data.get("release-groups", [])]
        if official_only:
            groups = [g for g in groups if g.is_official]
        return groups[:limit]

    def lookup_recording(self, mbid: str) -> MBRecording | None:
        if not self.enabled or not mbid:
            return None
        data = self._cached_get(
            ("lookup-recording", mbid),
            f"recording/{mbid}",
            {"inc": "artist-credits+releases+release-groups"},
        )
        if not data or "id" not in data:
            return None
        return _parse_recording(data)

    def lookup_release_tracks(
        self, release_mbid: str
    ) -> tuple[str, list[MBRecording]] | None:
        """The canonical title and ordered track list of one release.

        Unlike `lookup_release_group_tracks`, this takes an exact *release*
        MBID (the `mb_albumid` beets rows carry), not a release group — no
        re-selection of the canonical pressing is needed. Returns
        `(title, recordings-in-release-order)` or None when the release is
        unknown. The album-consolidation path (P6.9) uses it to re-tag to
        the canonical album title and renumber tracks from the release
        tracklist.
        """
        if not self.enabled or not release_mbid:
            return None
        data = self._cached_get(
            ("lookup-release", release_mbid),
            f"release/{release_mbid}",
            {"inc": "recordings+artist-credits"},
        )
        if not data or "id" not in data:
            return None
        return data.get("title") or "", _release_tracks(data)

    def lookup_release_group_tracks(self, release_group_mbid: str) -> list[MBRecording]:
        """The ordered track list of a release group's canonical release.

        Two lookups: the release group (to pick the canonical release), then
        that release's media/tracks. Returns [] rather than raising when the
        group is unknown (404) or has no usable release — an empty album is a
        legitimate "nothing to fetch", not a transport failure.
        """
        if not self.enabled or not release_group_mbid:
            return []
        group_data = self._cached_get(
            ("lookup-release-group", release_group_mbid),
            f"release-group/{release_group_mbid}",
            {"inc": "releases"},
        )
        if not group_data or "id" not in group_data:
            return []
        release = _select_canonical_release(group_data)
        if release is None:
            return []
        release_data = self._cached_get(
            ("lookup-release", release.mbid),
            f"release/{release.mbid}",
            {"inc": "recordings+artist-credits"},
        )
        if not release_data or "id" not in release_data:
            return []
        return _release_tracks(release_data)

    # -- the one the import path needs -------------------------------------

    def resolve_canonical(
        self, title: str, artist: str, min_score: int = 90
    ) -> MBRecording | None:
        """Turn "what the user asked for" into one canonical recording.

        The selection rule, in order:

        1. Discard anything MusicBrainz scored below `min_score`. A weak
           match applied silently is worse than no match, because the file
           still gets filed — just under a confident-looking wrong name.
        2. Prefer a recording that has a canonical studio release. This is
           what keeps `Kid A` from becoming `I Might Be Wrong` and
           `Homogenic` from becoming `LateNightTales`.
        3. Among those, prefer the highest score, then the earliest release.

        Returns None when nothing clears the bar. The caller **must** treat
        None as "import as-is and flag it", never as licence to guess.

        Step 1 has a documented exception: live-verified 2026-08-12 on
        Nujabes - Feather, MusicBrainz's own text relevance scored the real
        studio recording ("Nujabes featuring Cise Starr & Akin from CYNE")
        *below* `min_score`, purely for having a longer, more complete
        artist-credit string than a same-artist bootleg's plain "Nujabes"
        credit — the bootleg scored 100 and, being the only candidate left
        standing, won by default despite having no canonical studio release
        at all. See `_near_miss_studio_fallback`.
        """
        if not self.enabled:
            return None
        try:
            candidates = self.search_recording(
                title, artist, limit=25, canonical_only=True
            )
        except (MusicBrainzConnectionError, MusicBrainzRateLimitError) as e:
            # Deliberately swallowed to a None: MusicBrainz being down must
            # degrade the import to "unmatched", not fail the download that
            # already succeeded.
            logger.warning(
                "MusicBrainz resolve failed for %s - %s: %s", artist, title, e
            )
            return None

        confident = [c for c in candidates if (c.score or 0) >= min_score]
        if not confident:
            logger.info(
                "MusicBrainz had no confident match for '%s - %s' (best score %s)",
                artist,
                title,
                max((c.score or 0 for c in candidates), default=0),
            )
            return None

        studio = [c for c in confident if not c.is_live]
        if not studio:
            near_miss = self._near_miss_studio_fallback(candidates, min_score)
            if near_miss:
                studio = near_miss
        pool = studio or confident

        def sort_key(rec: MBRecording) -> tuple:
            """Score first, then penalise alternate editions, then age.

            The variant penalty only breaks ties the query filter left
            behind — "Madvillainy Instrumentals" and "Escapism:
            Instrumentals" both survive the filter legitimately, being
            official studio albums, and neither is what someone asking for
            the track meant.
            """
            release = rec.best_release
            variant = bool(release and looks_like_variant(release.title)) or (
                looks_like_variant(rec.title)
            )
            # Variant outranks score deliberately. Every candidate that
            # survives the canonical filter scores 99-100 — measured, not
            # assumed — so score carries almost no information at this point,
            # and sorting on it first let "Madvillainy Instrumentals" (100)
            # beat "Madvillainy" (99). Only tracks that already cleared
            # `min_score` reach here, so demoting on variant cannot promote a
            # genuinely bad match.
            return (
                variant,
                -(rec.score or 0),
                release.year if release and release.year else 9999,
            )

        best = min(pool, key=sort_key)
        logger.info(
            "MusicBrainz resolved '%s - %s' -> %s (%s, release=%s)",
            artist,
            title,
            best.title,
            best.mbid,
            best.best_release.title if best.best_release else "none",
        )
        return best

    @staticmethod
    def _near_miss_studio_fallback(
        candidates: list[MBRecording], min_score: int
    ) -> list[MBRecording]:
        """Candidates that scored just under `min_score` but do have a
        canonical studio release — considered only when nothing that
        actually cleared `min_score` has one either.

        Exists because MusicBrainz's own text relevance can rank a same-
        artist bootleg/remix above the real studio recording purely for
        having a shorter, less-complete artist-credit string, pushing the
        real recording a few points under the confidence bar. When that
        happens the bootleg becomes the *only* confident candidate and
        wins by default — a confidently-scored wrong answer, which is
        worse than the near-miss right one. See `resolve_canonical`'s
        docstring for the live-verified case.
        """
        return [
            c
            for c in candidates
            if not c.is_live
            and (c.score or 0) >= min_score - NEAR_MISS_STUDIO_SCORE_MARGIN
        ]
