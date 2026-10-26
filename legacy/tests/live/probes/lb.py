"""
`LbProbe` — ListenBrainz, called directly.

This exists so a test can know what a rec pull **should** have produced
before judging what it did produce. Without it, "musica pulled 5 Deep Cuts"
is unfalsifiable: nobody knows whether ListenBrainz had 5, 50 or 0 to give
that day.

Endpoint facts (BACKLOG.md "ListenBrainz API Reference", re-verified live
2026-08-12 against this user's account):

| Category     | Endpoint                                          | Pool |
|--------------|---------------------------------------------------|------|
| Comfort Zone | `/1/cf/recommendation/user/{user}/recording`       | `total_mbid_count`, 1000 here |
| Fresh Picks  | `/1/explore/fresh-releases/`                       | global, `days` max 90 |
| Deep Cuts    | `/1/user/{user}/playlists/recommendations`         | 1-2 playlists x <=50 tracks |

All three are public — **no token needed**, confirmed again while building
this. Comfort Zone's live route must be called without a trailing slash; the
trailing-slash variant returned HTTP 404 on 2026-08-13.

Two things worth knowing before comparing a pull against this:

- **Comfort Zone returns MBIDs only** — no artist, no title. Turning those
  into names costs one MusicBrainz lookup each (rate-limited to ~1/s), so
  resolution is opt-in via `resolve=True`. Compare by MBID when you can.
- **Fresh Picks entries carry no `recording_mbid`** — none of the 1535
  releases in the 7-day window had one. `app/services/recommendation.py`
  reads `release.get("recording_mbid")` for its `mbid`, so every Fresh Pick
  musica produces has `mbid=None` by construction. `release_mbid` and
  `release_group_mbid` are the ids that actually exist, and both are
  surfaced here.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from tests.live.probes.contract import LbProbe
from tests.live.probes.paths import env_value, read_config

DEFAULT_LB_URL = "https://api.listenbrainz.org"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "musica-live-probe/1.0 (pipeline measurement)"


class LbProbeError(RuntimeError):
    """ListenBrainz answered with something unusable."""


class LiveLbProbe(LbProbe):
    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        configured = (read_config().get("listenbrainz", {}) or {}).get("url")
        self.base_url = (base_url or configured or DEFAULT_LB_URL).rstrip("/")
        self.username = username or env_value("LISTENBRAINZ_USERNAME")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    # -- plumbing ----------------------------------------------------------

    def _get(self, url: str, retries: int = 3, **params: Any) -> dict:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self._session.get(
                    url, params=params or None, timeout=self.timeout, allow_redirects=True
                )
            except requests.RequestException as exc:
                last = exc
                time.sleep(1.0 + attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = LbProbeError(f"{url}: HTTP {resp.status_code}")
                time.sleep(2.0 + 2 * attempt)
                continue
            if resp.status_code != 200:
                raise LbProbeError(f"{url}: HTTP {resp.status_code} {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise LbProbeError(f"{url}: unparseable JSON") from exc
        raise LbProbeError(f"{url}: unreachable after {retries} tries ({last})")

    # -- comfort zone ------------------------------------------------------

    def comfort_zone_meta(self) -> dict:
        """The payload envelope minus the MBIDs — `total_mbid_count` (the
        real pool size) and `last_updated` (the only rotation signal there
        is; LB documents no regeneration schedule)."""
        payload = self._get(
            f"{self.base_url}/1/cf/recommendation/user/{self.username}/recording",
            count=1,
        ).get("payload", {})
        return {k: v for k, v in payload.items() if k != "mbids"}

    def comfort_zone(self, count: int, resolve: bool = False) -> list[dict]:
        """The first `count` Comfort Zone recommendations, in LB's order.

        Each dict carries `mbid`, `score` and `latest_listened_at` (null
        for tracks the user has never played — CF blends heard and unheard,
        so this is *not* a "only stuff you know" feed). `artist` and `track`
        are None unless `resolve=True`, which costs one throttled
        MusicBrainz lookup per track.
        """
        payload = self._get(
            f"{self.base_url}/1/cf/recommendation/user/{self.username}/recording",
            count=count,
        ).get("payload", {})
        recs: list[dict] = []
        for entry in payload.get("mbids", [])[:count]:
            mbid = entry.get("recording_mbid", "")
            rec = {
                "source": "comfort_zone",
                "mbid": mbid,
                "score": entry.get("score"),
                "latest_listened_at": entry.get("latest_listened_at"),
                "artist": None,
                "track": None,
                "raw": entry,
            }
            if resolve and mbid:
                meta = self.recording_metadata(mbid)
                rec["artist"] = meta.get("artist")
                rec["track"] = meta.get("title")
            recs.append(rec)
        return recs

    def recording_metadata(self, mbid: str) -> dict:
        """Resolve one recording MBID against MusicBrainz.

        Sleeps a second first: MusicBrainz enforces ~1 request/second and
        answers 503 to bursts, which would otherwise read as "the track
        doesn't exist".
        """
        time.sleep(1.0)
        try:
            data = self._get(
                f"{MUSICBRAINZ_URL}/recording/{mbid}",
                fmt="json",
                inc="artist-credits+releases",
            )
        except LbProbeError:
            return {}
        credits = data.get("artist-credit") or []
        artist = "".join(
            (c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "")
            for c in credits
            if isinstance(c, dict)
        ).strip()
        releases = data.get("releases") or []
        return {
            "mbid": mbid,
            "title": data.get("title", ""),
            "artist": artist,
            "album": releases[0].get("title") if releases else None,
        }

    # -- fresh picks -------------------------------------------------------

    def fresh_picks(self, days: int) -> list[dict]:
        """The whole fresh-releases pool for a window, newest release first.

        Returns the *pool*, not a slice: what a correct pull produces is
        this list ordered by `release_date` descending, then filtered for
        spoken word, then cut to N. Handing tests the full pool lets them
        check the ordering and the filtering separately.

        This feed is global, not personalised (accepted behaviour, user
        decision 2026-08-10), and `listen_count` is 0 on every entry, so it
        cannot be used for ranking.
        """
        payload = self._get(
            f"{self.base_url}/1/explore/fresh-releases/", days=days
        ).get("payload", {})
        releases = payload.get("releases", [])
        releases = sorted(
            releases, key=lambda r: r.get("release_date") or "", reverse=True
        )
        return [
            {
                "source": "fresh_picks",
                "artist": release.get("artist_credit_name", ""),
                "track": release.get("release_name", ""),
                "album": release.get("release_name", ""),
                # Deliberately None: the feed has no recording_mbid at all.
                "mbid": release.get("recording_mbid"),
                "release_mbid": release.get("release_mbid"),
                "release_group_mbid": release.get("release_group_mbid"),
                "release_date": release.get("release_date"),
                "primary_type": release.get("release_group_primary_type"),
                "secondary_type": release.get("release_group_secondary_type"),
                "raw": release,
            }
            for release in releases
        ]

    # -- deep cuts ---------------------------------------------------------

    def deep_cuts_playlists(self) -> list[dict]:
        """The recommendation playlists themselves — id, title, track count.

        The listing endpoint returns **no tracks**; `track` is an empty list
        on every entry and each playlist has to be fetched by UUID. Anything
        counting tracks off this response counts zero.
        """
        data = self._get(
            f"{self.base_url}/1/user/{self.username}/playlists/recommendations"
        )
        playlists = []
        for entry in data.get("playlists", []):
            inner = entry.get("playlist", {})
            identifier = inner.get("identifier", "")
            uuid = identifier.rsplit("/", 1)[-1] if identifier else ""
            playlists.append(
                {
                    "id": uuid,
                    "identifier": identifier,
                    "title": inner.get("title", ""),
                    "date": inner.get("date"),
                    "raw": inner,
                }
            )
        return playlists

    def playlist_tracks(self, playlist_id: str) -> list[dict]:
        data = self._get(f"{self.base_url}/1/playlist/{playlist_id}")
        return data.get("playlist", {}).get("track", [])

    def deep_cuts(self) -> list[dict]:
        """Every track across every current recommendation playlist.

        The pool, again — not a slice. `count` in musica's config is a
        number of *tracks*, and the bug this makes visible (a count of 5
        pulling 5 whole playlists, ~100 tracks, on 2026-08-11) is only
        detectable if the test knows how many tracks were on offer.
        """
        tracks: list[dict] = []
        for playlist in self.deep_cuts_playlists():
            if not playlist["id"]:
                continue
            for track in self.playlist_tracks(playlist["id"]):
                identifier = track.get("identifier")
                if isinstance(identifier, list):
                    identifier = identifier[0] if identifier else ""
                mbid = (
                    identifier.rsplit("/", 1)[-1]
                    if isinstance(identifier, str) and "musicbrainz.org" in identifier
                    else None
                )
                tracks.append(
                    {
                        "source": "deep_cuts",
                        "artist": track.get("creator", ""),
                        "track": track.get("title", ""),
                        "album": track.get("album"),
                        "mbid": mbid,
                        "playlist_id": playlist["id"],
                        "playlist_title": playlist["title"],
                        "raw": track,
                    }
                )
        return tracks
