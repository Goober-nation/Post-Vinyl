"""
ListenBrainzRecs — Concrete implementation of RecommendationService using ListenBrainz API.

Fetches recommendations from ListenBrainz, classifies against library, queues downloads.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from app.config import Config
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError,
    RecommendationFetchError,
)
from app.logging_config import get_logger
from app.services.interfaces.recommendation import (
    Classification,
    Recommendation,
    RecommendationService,
)
from app.services.library import Song
from app.services.query_builder import (
    REMIX_QUALIFIERS,
    STOP_WORDS,
    fold_for_matching,
    strip_feat,
)

logger = get_logger(__name__)


def normalize_text(text: str) -> str:
    """Normalize text for identity comparison (alnum-only, lowercased).

    Module-level so callers outside RecommendationService (e.g. RecPuller's
    ledger dedup) can build the same identity key without depending on a
    RecommendationService instance.
    """
    if not text:
        return ""
    return "".join(c.lower() for c in text if c.isalnum())


# Non-music release types to keep out of recommendations. These are
# MusicBrainz release-group types, surfaced verbatim by LB's fresh-releases
# payload — far more reliable than guessing from a title. Verified live
# 2026-08-11: podcasts come through as primary type "Broadcast" (that's how
# "The Adam Buxton Podcast #279" and three others arrived in a real pull).
SPOKEN_WORD_PRIMARY_TYPES = {"broadcast"}
SPOKEN_WORD_SECONDARY_TYPES = {
    "audiobook",
    "audio drama",
    "spokenword",
    "interview",
}

# Fallback for releases MusicBrainz hasn't typed (a podcast filed as
# "Album" would otherwise slip through). Deliberately narrow: it runs on
# titles, so anything looser produces false positives — "One Assassination
# Under God, Chapter 2" (Marilyn Manson) is a real fresh-release that a
# regex including "chapter" or "episode" would wrongly drop.
SPOKEN_WORD_NAME_RE = re.compile(
    r"(?i)(?:\bpodcasts?\b|\bpod\.|\baudio\s?books?\b|\bhörbuch\b)"
)


def is_spoken_word(
    name: str = "",
    artist: str = "",
    primary_type: str | None = None,
    secondary_type: str | None = None,
) -> bool:
    """True if a release looks like a podcast/audiobook rather than music.

    Type metadata wins when present; the name check is only a fallback for
    untyped releases.
    """
    if (primary_type or "").strip().lower() in SPOKEN_WORD_PRIMARY_TYPES:
        return True
    if (secondary_type or "").strip().lower() in SPOKEN_WORD_SECONDARY_TYPES:
        return True
    return bool(SPOKEN_WORD_NAME_RE.search(f"{artist} {name}"))


class ListenBrainzRecs(RecommendationService):
    """
    ListenBrainz-based recommendation implementation.

    Fetches recommendations from ListenBrainz API:
    - Comfort Zone: User-based collaborative filtering
    - Fresh Picks: New releases
    - Deep Cuts: Playlist-based recommendations
    """

    def __init__(self, config: Config):
        """
        Initialize ListenBrainzRecs.

        Args:
            config: Config object with ListenBrainz settings
        """
        self.config = config
        self.base_url = config.listenbrainz.url
        self.token = config.listenbrainz.token
        self.username = config.listenbrainz.username
        self.session = requests.Session()
        self._state_store = None
        self._comfort_state = {
            "offset": 0,
            "last_updated": None,
            "total_count": None,
            "warning": None,
        }
        self._deep_ingested_playlists: set[str] = set()
        self._deep_pool: list[dict] = []
        self._category_warnings: dict[str, str] = {}

        # Stop words for artist filtering — shared with the query pipeline
        # (P6.5-6), kept as attributes for existing callers.
        self.stop_words = STOP_WORDS

        # Remix qualifiers to exclude — shared with the query pipeline.
        self.remix_qualifiers = REMIX_QUALIFIERS

        # Valid audio extensions
        self.valid_audio_exts = {".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac"}

    def set_state_store(self, store) -> None:
        """Attach the SQLite-backed category state used by the worker."""
        self._state_store = store

    def category_warnings(self) -> dict[str, str]:
        """Return warnings produced by the category fill mechanics."""
        if self._state_store is not None:
            return self._state_store.category_warnings()
        return dict(self._category_warnings)

    def _category_state(self, category: str) -> dict:
        if self._state_store is not None:
            return self._state_store.get_category_state(category)
        if category == "comfort_zone":
            return dict(self._comfort_state)
        return {"offset": 0, "last_updated": None, "total_count": None, "warning": None}

    def _save_category_state(self, category: str, state: dict) -> None:
        if self._state_store is not None:
            self._state_store.set_category_state(
                category,
                offset=state.get("offset", 0),
                last_updated=state.get("last_updated"),
                total_count=state.get("total_count"),
                warning=state.get("warning"),
            )
        elif category == "comfort_zone":
            self._comfort_state = dict(state)

        warning = state.get("warning")
        if warning:
            self._category_warnings[category] = warning
        else:
            self._category_warnings.pop(category, None)

    def _set_category_warning(self, category: str, warning: str | None) -> None:
        state = self._category_state(category)
        state["warning"] = warning
        self._save_category_state(category, state)

    def _get_headers(self) -> dict:
        """Get HTTP headers for ListenBrainz API."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers

    def fetch_recommendations(self, counts: dict[str, int]) -> list[Recommendation]:
        """
        Fetch recommendations from ListenBrainz.

        Args:
            counts: Dict mapping source name to count

        Returns:
            List of Recommendation objects
        """
        if not self.config.listenbrainz.enabled:
            raise ListenBrainzDisabledError()

        logger.info(f"Fetching recommendations: {counts}")

        all_recs = []

        # Fetch Comfort Zone
        comfort_count = counts.get("comfort_zone", 0)
        if comfort_count > 0:
            comfort_recs = self._fetch_comfort_zone(comfort_count)
            all_recs.extend(comfort_recs)

        # Fetch Fresh Picks
        fresh_count = counts.get("fresh_picks", 0)
        if fresh_count > 0:
            fresh_recs = self._fetch_fresh_picks(fresh_count)
            all_recs.extend(fresh_recs)

        # Fetch Deep Cuts
        deep_count = counts.get("deep_cuts", 0)
        if deep_count > 0:
            deep_recs = self._fetch_deep_cuts(deep_count)
            all_recs.extend(deep_recs)

        logger.info(f"Fetched {len(all_recs)} total recommendations")
        return all_recs

    def _fetch_comfort_zone(self, count: int) -> list[Recommendation]:
        """Fetch the next page from Comfort Zone's current CF model pool."""
        logger.debug("Fetching %d Comfort Zone recommendations", count)

        url = f"{self.base_url}/1/cf/recommendation/user/{self.username}/recording"
        state = self._category_state("comfort_zone")
        previous_last_updated = state.get("last_updated")
        total_hint = self._as_int(state.get("total_count"))
        requested_offset = self._as_int(state.get("offset")) or 0
        warning: str | None = None
        if total_hint is not None and requested_offset >= total_hint:
            requested_offset = 0
            warning = (
                "Comfort Zone's recommendation pool was exhausted; "
                "the next page repeats tracks from the beginning."
            )

        try:
            data = self._request_comfort_zone(url, count, requested_offset)
            payload = data.get("payload")
            if not isinstance(payload, dict):
                raise RecommendationFetchError(
                    "comfort_zone", "response payload is not an object"
                )
            mbids = payload.get("mbids")
            if not isinstance(mbids, list):
                raise RecommendationFetchError(
                    "comfort_zone", "response payload has no mbids list"
                )

            response_last_updated = payload.get("last_updated")
            response_last_key = (
                str(response_last_updated)
                if response_last_updated is not None
                else None
            )
            # The model generation is only visible in the page response. If
            # it changed since the previous pull, discard that page and ask
            # again from the beginning of the new pool.
            if (
                previous_last_updated is not None
                and response_last_key is not None
                and response_last_key != str(previous_last_updated)
            ):
                logger.info(
                    "Comfort Zone model changed from %s to %s; resetting offset",
                    previous_last_updated,
                    response_last_key,
                )
                requested_offset = 0
                warning = None
                data = self._request_comfort_zone(url, count, requested_offset)
                payload = data.get("payload")
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("mbids"), list
                ):
                    raise RecommendationFetchError(
                        "comfort_zone", "reset response has no mbids list"
                    )
                mbids = payload["mbids"]
                response_last_updated = payload.get("last_updated")
                response_last_key = (
                    str(response_last_updated)
                    if response_last_updated is not None
                    else response_last_key
                )

            total_count = self._as_int(payload.get("total_mbid_count"))
            if total_count is None:
                total_count = (
                    max(requested_offset + len(mbids), total_hint or 0) or None
                )

            page = mbids[:count]
            next_offset = requested_offset + len(page)
            if total_count is not None and next_offset >= total_count:
                next_offset = 0
                warning = (
                    "Comfort Zone's recommendation pool was exhausted; "
                    "the next page repeats tracks from the beginning."
                )

            state.update(
                {
                    "offset": next_offset,
                    "last_updated": response_last_key or previous_last_updated,
                    "total_count": total_count,
                    "warning": warning,
                }
            )
            self._save_category_state("comfort_zone", state)

            recs = []
            for item in page:
                if not isinstance(item, dict):
                    continue
                mbid = str(item.get("recording_mbid") or "").strip()
                if not mbid:
                    continue
                metadata = self._fetch_recording_metadata(mbid)
                recs.append(
                    Recommendation(
                        source="comfort_zone",
                        artist=metadata.get("artist_name", ""),
                        track=metadata.get("track_name", ""),
                        mbid=mbid,
                    )
                )

            logger.info("Fetched %d Comfort Zone recommendations", len(recs))
            return recs

        except requests.exceptions.RequestException as e:
            logger.error("Comfort Zone connection error: %s", e)
            raise ListenBrainzConnectionError(self.base_url, str(e))

    def _request_comfort_zone(self, url: str, count: int, offset: int) -> dict:
        """Request a Comfort Zone page using LB's no-trailing-slash route."""
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            params={"count": count, "offset": offset},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RecommendationFetchError("comfort_zone", f"HTTP {resp.status_code}")
        return resp.json()

    @staticmethod
    def _as_int(value) -> int | None:
        """Parse a numeric API/state field without trusting its shape."""
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _fetch_fresh_picks(self, count: int) -> list[Recommendation]:
        """Fetch a rolling-window candidate batch from Fresh Releases."""
        logger.debug("Fetching %d Fresh Picks recommendations", count)

        url = f"{self.base_url}/1/explore/fresh-releases/"
        settings = getattr(self.config, "fresh_picks", None)
        configured = settings is not None
        pull_window_seconds = (
            int(getattr(settings, "window_seconds", 0)) if configured else 0
        )
        api_days = int(getattr(settings, "window_days", 0)) if configured else None
        offset = int(getattr(settings, "offset", 0)) if configured else 0
        search_buffer = int(getattr(settings, "search_buffer", 0)) if configured else 0
        candidate_count = max(0, count + search_buffer)

        try:
            params = {"days": max(1, api_days)} if api_days else None
            resp = self.session.get(url, params=params, timeout=30)

            if resp.status_code != 200:
                raise RecommendationFetchError(
                    "fresh_picks", f"HTTP {resp.status_code}"
                )

            data = resp.json()
            releases = data.get("payload", {}).get("releases", [])

            # LB returns this feed sorted **alphabetically by artist**, so
            # taking releases[:count] wasn't picking the freshest releases
            # at all — it picked the same alphabetical head every single
            # pull ("144p", "171", "2XT"…), which is both repetitive and
            # the most obscure end of the list. Sort newest-first instead,
            # which is what "fresh releases" is supposed to mean.
            # (`listen_count` would be the better ranking signal, but it is
            # 0 on every single release — verified 2026-08-11 across all
            # 31,010 entries in the 90-day window — so it is unusable.)
            releases = sorted(
                releases, key=lambda r: r.get("release_date") or "", reverse=True
            )

            if configured:
                releases = releases[max(0, offset) :]
                cutoff = datetime.now(timezone.utc) - timedelta(
                    seconds=pull_window_seconds
                )
                releases = [
                    release
                    for release in releases
                    if self._release_is_in_window(release.get("release_date"), cutoff)
                ]

            recs: list[Recommendation] = []
            excluded = 0
            for release in releases:
                if len(recs) >= candidate_count:
                    break
                artist = release.get("artist_credit_name", "")
                track = release.get("release_name", "")
                mbid = release.get("recording_mbid", "")
                album = release.get("release_name", "")

                # Fresh Picks is LB's *global* new-releases feed, so it
                # carries whatever MusicBrainz ingested — podcasts and
                # audiobooks included. Filtering before the count slice
                # matters: otherwise a podcast doesn't just waste a slot,
                # it burns one of the user's N picks on a guaranteed
                # search failure.
                if is_spoken_word(
                    name=track,
                    artist=artist,
                    primary_type=release.get("release_group_primary_type"),
                    secondary_type=release.get("release_group_secondary_type"),
                ):
                    excluded += 1
                    logger.debug(
                        "Fresh Picks: skipped non-music release (%s / %s): %s — %s",
                        release.get("release_group_primary_type"),
                        release.get("release_group_secondary_type"),
                        artist,
                        track,
                    )
                    continue

                rec = Recommendation(
                    source="fresh_picks",
                    artist=artist,
                    track=track,
                    mbid=mbid if mbid else None,
                    album=album,
                    release_mbid=release.get("release_mbid"),
                )
                recs.append(rec)

            if excluded:
                logger.info(
                    "Fresh Picks: excluded %d non-music release(s) "
                    "(podcast/audiobook/spoken word)",
                    excluded,
                )
            logger.info(f"Fetched {len(recs)} Fresh Picks recommendations")
            return recs

        except requests.exceptions.RequestException as e:
            logger.error(f"Fresh Picks connection error: {e}")
            raise ListenBrainzConnectionError(self.base_url, str(e))

    @staticmethod
    def _release_is_in_window(value: str | None, cutoff: datetime) -> bool:
        """Compare LB's date-only or ISO release date defensively."""
        if not value:
            return False
        try:
            text = str(value)
            if len(text) == 10:
                release_at = datetime.strptime(text, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            else:
                release_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if release_at.tzinfo is None:
                    release_at = release_at.replace(tzinfo=timezone.utc)
            return release_at >= cutoff
        except (TypeError, ValueError):
            return False

    def _fetch_deep_cuts(self, count: int) -> list[Recommendation]:
        """Serve ``count`` tracks from the persisted Deep Cuts pool."""
        logger.debug("Fetching %d Deep Cuts recommendations", count)

        url = f"{self.base_url}/1/user/{self.username}/playlists/recommendations"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=30)

            if resp.status_code != 200:
                raise RecommendationFetchError("deep_cuts", f"HTTP {resp.status_code}")

            data = resp.json()
            playlists = data.get("playlists")
            if not isinstance(playlists, list):
                raise RecommendationFetchError(
                    "deep_cuts", "response has no playlists list"
                )

            for entry in playlists:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("playlist")
                if not isinstance(inner, dict):
                    logger.warning("Deep Cuts: skipped malformed playlist entry")
                    continue
                playlist_id = self._playlist_uuid(inner.get("identifier"))
                if not playlist_id:
                    logger.warning(
                        "Deep Cuts: could not extract playlist UUID from %s",
                        inner.get("identifier"),
                    )
                    continue

                if self._state_store is not None:
                    already_ingested = self._state_store.is_deep_cuts_playlist_ingested(
                        playlist_id
                    )
                else:
                    already_ingested = playlist_id in self._deep_ingested_playlists
                if already_ingested:
                    continue

                tracks = self._fetch_playlist_tracks(playlist_id)
                if not tracks:
                    logger.warning(
                        "Deep Cuts: playlist %s returned no tracks; will retry it",
                        playlist_id,
                    )
                    continue
                parsed_tracks = self._parse_deep_cuts_tracks(tracks)
                if self._state_store is not None:
                    self._state_store.ingest_deep_cuts_playlist(
                        playlist_id,
                        title=inner.get("title"),
                        playlist_date=inner.get("date"),
                        tracks=parsed_tracks,
                    )
                else:
                    self._deep_ingested_playlists.add(playlist_id)
                    self._deep_pool.extend(parsed_tracks)

            if self._state_store is not None:
                pool_rows = self._state_store.take_deep_cuts_tracks(count)
                recs = [
                    Recommendation(
                        source="deep_cuts",
                        artist=row["artist"],
                        track=row["track"],
                        mbid=row["mbid"],
                        album=row["album"],
                    )
                    for row in pool_rows
                ]
            else:
                recs = self._take_in_memory_deep_cuts(count)

            if recs:
                self._set_category_warning("deep_cuts", None)
            else:
                self._set_category_warning(
                    "deep_cuts",
                    "Deep Cuts has no unserved tracks; waiting for a new ListenBrainz playlist.",
                )
            logger.info("Fetched %d Deep Cuts recommendations", len(recs))
            return recs

        except requests.exceptions.RequestException as e:
            logger.error(f"Deep Cuts connection error: {e}")
            raise ListenBrainzConnectionError(self.base_url, str(e))

    @staticmethod
    def _playlist_uuid(identifier) -> str:
        """Extract a UUID only from a ListenBrainz playlist URL."""
        if not isinstance(identifier, str):
            return ""
        prefix = "https://listenbrainz.org/playlist/"
        if not identifier.startswith(prefix):
            return ""
        return identifier.rstrip("/").rsplit("/", 1)[-1]

    def _parse_deep_cuts_tracks(self, tracks: list[dict]) -> list[dict]:
        """Convert LB JSPF tracks to the local pool shape."""
        parsed: list[dict] = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            artist = str(track.get("creator") or "").strip()
            title = str(track.get("title") or "").strip()
            if not title or is_spoken_word(name=title, artist=artist):
                continue
            identifier = track.get("identifier")
            if isinstance(identifier, list):
                identifier = identifier[0] if identifier else ""
            mbid = (
                identifier.rsplit("/", 1)[-1]
                if isinstance(identifier, str) and "musicbrainz.org" in identifier
                else None
            )
            parsed.append(
                {
                    "artist": artist,
                    "track": title,
                    "album": track.get("album"),
                    "mbid": mbid,
                }
            )
        return parsed

    def _take_in_memory_deep_cuts(self, count: int) -> list[Recommendation]:
        """Fallback pool for direct service use without a database."""
        recs: list[Recommendation] = []
        seen: set[str] = set()
        remaining: list[dict] = []
        for track in self._deep_pool:
            key = (
                f"mbid:{track['mbid'].casefold()}"
                if track.get("mbid")
                else f"name:{track['artist'].casefold()}::{track['track'].casefold()}"
            )
            if key in seen:
                continue
            if len(recs) < count:
                seen.add(key)
                recs.append(
                    Recommendation(
                        source="deep_cuts",
                        artist=track["artist"],
                        track=track["track"],
                        mbid=track.get("mbid"),
                        album=track.get("album"),
                    )
                )
            else:
                remaining.append(track)
        self._deep_pool = remaining
        return recs

    def _fetch_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Fetch tracks from a ListenBrainz playlist."""
        url = f"{self.base_url}/1/playlist/{playlist_id}"

        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=30)

            if resp.status_code != 200:
                return []

            data = resp.json()
            return data.get("playlist", {}).get("track", [])

        except (
            requests.exceptions.RequestException,
            ValueError,
            TypeError,
            AttributeError,
        ) as e:
            logger.warning(f"Failed to fetch playlist tracks: {e}")
            return []

    def _fetch_recording_metadata(self, mbid: str) -> dict:
        """Fetch recording metadata from MusicBrainz."""
        url = f"https://musicbrainz.org/ws/2/recording/{mbid}?fmt=json&inc=artist-credits+releases"
        headers = {"User-Agent": "musica/1.0"}

        try:
            resp = self.session.get(url, headers=headers, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                artist_name = ""
                track_name = data.get("title", "")

                if data.get("artist-credit"):
                    artist_name = (
                        data["artist-credit"][0].get("artist", {}).get("name", "")
                    )

                return {"artist_name": artist_name, "track_name": track_name}
            elif resp.status_code == 503:
                # Rate limited, wait and retry
                logger.warning("MusicBrainz rate-limited, waiting 5s")
                time.sleep(5)
                resp = self.session.get(url, headers=headers, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    artist_name = ""
                    track_name = data.get("title", "")
                    if data.get("artist-credit"):
                        artist_name = (
                            data["artist-credit"][0].get("artist", {}).get("name", "")
                        )
                    return {"artist_name": artist_name, "track_name": track_name}

            return {"artist_name": "", "track_name": ""}

        except (
            requests.exceptions.RequestException,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
        ) as e:
            logger.warning(f"Failed to fetch recording metadata for {mbid}: {e}")
            return {"artist_name": "", "track_name": ""}

    def classify(
        self, recs: list[Recommendation], library: list[Song]
    ) -> Classification:
        """
        Classify recommendations against library.

        Args:
            recs: List of Recommendation objects
            library: List of Song objects from library

        Returns:
            Classification with in_library, to_download, and skipped lists
        """
        logger.info(
            f"Classifying {len(recs)} recommendations against {len(library)} library songs"
        )

        in_library = []
        to_download = []
        skipped: list[Recommendation] = []

        seen_mbids = set()
        seen_keys = set()

        for rec in recs:
            # Deduplicate
            if rec.mbid and rec.mbid in seen_mbids:
                continue

            key = f"{self._normalize(rec.artist)}::{self._normalize(rec.track)}"
            if key in seen_keys:
                continue

            if rec.mbid:
                seen_mbids.add(rec.mbid)
            seen_keys.add(key)

            # Try to match against library
            match = self._find_library_match(rec, library)

            if match:
                in_library.append(rec)
            else:
                to_download.append(rec)

        logger.info(
            f"Classification: {len(in_library)} in library, {len(to_download)} to download, {len(skipped)} skipped"
        )

        return Classification(
            in_library=in_library, to_download=to_download, skipped=skipped
        )

    def _find_library_match(
        self, rec: Recommendation, library: list[Song]
    ) -> Song | None:
        """Find a matching song in the library."""
        # 1. Exact MBID match
        if rec.mbid:
            for song in library:
                if song.mbid and song.mbid.lower() == rec.mbid.lower():
                    logger.debug(f"Matched by MBID: {rec.artist} - {rec.track}")
                    return song

        # 2. Normalized artist + track match
        rec_key = f"{self._normalize(rec.artist)}::{self._normalize(rec.track)}"
        for song in library:
            song_key = f"{self._normalize(song.artist)}::{self._normalize(song.title)}"
            if song_key == rec_key:
                logger.debug(f"Matched by artist+track: {rec.artist} - {rec.track}")
                return song

        # 3. Filename match
        rec_filename = self._normalize(rec.track)
        for song in library:
            song_filename = self._normalize(os.path.basename(song.path))
            if song_filename == rec_filename:
                logger.debug(f"Matched by filename: {rec.artist} - {rec.track}")
                return song

        return None

    def _normalize(self, text: str) -> str:
        """Normalize text for comparison."""
        return normalize_text(text)

    def queue_downloads(self, recs: list[Recommendation]) -> dict:
        """
        Queue downloads for recommendations.

        Note: This is a stub. Actual implementation requires integration with
        SlskdDownload and SlskdSearch, which would create circular dependencies.
        The actual queueing logic is handled by the recommendation pipeline worker.

        Args:
            recs: List of Recommendation objects to download

        Returns:
            Dict with queued, failed, and failures
        """
        # This is a placeholder. In the actual implementation, this would be
        # called by the RecPuller worker which has access to both search and download services.

        logger.warning(
            "queue_downloads() called directly on ListenBrainzRecs — this is a stub"
        )

        return {
            "queued": 0,
            "failed": len(recs),
            "failures": [
                {
                    "artist": rec.artist,
                    "track": rec.track,
                    "message": "Direct queueing not supported",
                }
                for rec in recs
            ],
        }

    def _artist_words(self, artist_name: str) -> list[str]:
        """Extract meaningful words from artist name.

        Feat-clause truncated first (P6.5-6): "Alesso feat. Katy Perry"
        must become "Alesso" before word-selection, or the featured
        artist's name can win the longest-word pick.

        Accent-folded and period-stripped (2026-08-12) via the shared
        `fold_for_matching` — Python's `\\w` is Unicode-aware, so this
        function's own regex never shattered "Björk" the way
        `query_builder.select_words` used to, but it also never stripped
        the accent, which is its own bug: `_filepath_contains_artist` below
        does a plain substring check, and most Soulseek peers spell
        filenames without accents. An unfolded "björk" would silently
        reject a correct match on an ordinary "01 Bjork - Joga.flac".
        """
        cleaned = re.sub(
            r"[^\w\s.!?&]", " ", fold_for_matching(strip_feat(artist_name))
        )
        tokens = cleaned.lower().split()

        meaningful = []
        for t in tokens:
            if t in self.stop_words:
                continue
            if len(t) < 2:
                continue
            if t.isdigit():
                continue
            meaningful.append(t)

        return meaningful

    def _filepath_contains_artist(self, filepath: str, artist_words: list[str]) -> bool:
        """Check if filepath contains artist words.

        The filepath is accent-folded too (2026-08-12), not just the words
        being searched for. `artist_words` already come out of
        `_artist_words` folded to ASCII ("björk" -> "bjork"); a peer who
        *did* keep the accent in their filename ("01 Björk - Jóga.flac")
        would otherwise never match a folded word, since "bjork" is not a
        substring of "björk" either direction. Folding both sides means the
        comparison happens in one consistent space regardless of which way
        a given peer spelled it.
        """
        if not artist_words:
            return True

        lower = fold_for_matching(filepath).lower()
        return any(word in lower for word in artist_words)

    def _filename_has_remix_qualifier(self, filename: str) -> bool:
        """Check if filename has remix qualifier."""
        fname_lower = filename.lower()
        return any(q in fname_lower for q in self.remix_qualifiers)
