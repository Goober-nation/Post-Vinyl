"""
SearchesPlaylistService — gets a completed manual (non-rec) download into
the "Searches" playlist (#19).

Mirrors RecPlaylistService: found by role and ID-tracked (see
app.services.playlist_registry), created lazily if missing. Covers both
search modes/beets profiles the issue named — MB-tab ("library" profile)
and direct Soulseek-tab ("searches" profile) downloads both persist their
query as a `searches` row (see mb_resolver.py / routes/search.py), so title
and artist are available uniformly regardless of which profile the file
landed in.
"""

from app.logging_config import get_logger
from app.services.playlist_registry import resolve_playlist_id
from app.services.recommendation import normalize_text

logger = get_logger(__name__)

SEARCHES_ROLE = "searches"
SEARCHES_PLAYLIST_NAME = "Searches"


class SearchesPlaylistService:
    """Adds completed manual-download tracks to the Searches playlist."""

    def __init__(self, config, library_service, download_store, playlist_store):
        self._config = config
        self._library = library_service
        self._store = download_store
        self._playlist_store = playlist_store

    def _find_or_create_playlist(self) -> str | None:
        try:
            existing = self._library.list_playlists()
        except Exception:  # noqa: BLE001 — library backends vary
            logger.warning("SearchesPlaylist: list_playlists failed, assuming none")
            existing = []
        return resolve_playlist_id(
            role=SEARCHES_ROLE,
            desired_name=SEARCHES_PLAYLIST_NAME,
            existing=existing,
            store=self._playlist_store,
            library_service=self._library,
            create_if_missing=True,
        )

    def _find_song(self, title: str | None, artist: str | None):
        """Same fuzzy title/artist match RecPlaylistService uses — beets may
        have replaced the peer's own tags with resolved MusicBrainz
        metadata during import, so an exact string match can't be assumed."""
        queries = [q for q in (title, artist) if q]
        if not queries:
            return None

        songs = []
        seen_ids: set[str] = set()
        for query in queries:
            try:
                matches = self._library.search_library(query)
            except Exception:  # noqa: BLE001 — library backends vary
                logger.warning("SearchesPlaylist: library probe failed for %s", query)
                continue
            for song in matches:
                if song.song_id and song.song_id not in seen_ids:
                    songs.append(song)
                    seen_ids.add(song.song_id)

        target_title = normalize_text(title or "")
        target_artist = normalize_text(artist or "")
        for song in songs:
            if target_title and normalize_text(song.title) == target_title:
                if not target_artist or normalize_text(song.artist) == target_artist:
                    return song
        artist_matches = [
            song
            for song in songs
            if target_artist
            and (
                normalize_text(song.artist) == target_artist
                or target_artist in normalize_text(song.artist)
                or normalize_text(song.artist) in target_artist
            )
        ]
        if len(artist_matches) == 1:
            return artist_matches[0]
        return None

    def add_downloaded_to_playlist(
        self, transfer_id: str, title: str | None, artist: str | None
    ) -> bool:
        """Add one completed manual download's track to Searches.

        Returns True when the track is in the playlist (added now, or
        already there) — the row's playlist_id is then recorded. False
        means "nothing to add yet" (index lag, unresolvable song, or the
        playlist itself couldn't be found/created) and the caller should
        retry on a later poll.
        """
        if not getattr(getattr(self._config, "navidrome", None), "enabled", True):
            return False

        playlist_id = self._find_or_create_playlist()
        if playlist_id is None:
            return False

        song = self._find_song(title, artist)
        if song is None:
            logger.info(
                "SearchesPlaylist: %s - %s not found in library yet (index "
                "lag?), will retry",
                artist,
                title,
            )
            return False

        try:
            detail = self._library.get_playlist_detail(playlist_id)
            already = {s.song_id for s in detail.songs if s.song_id}
        except Exception:  # noqa: BLE001 — playlist backends vary
            logger.warning(
                "SearchesPlaylist: get_playlist_detail failed for %s", playlist_id
            )
            return False
        if song.song_id in already:
            self._store.set_playlist(transfer_id, playlist_id)
            return True

        try:
            ok = self._library.add_to_playlist(playlist_id, [song.song_id])
        except Exception as e:  # noqa: BLE001 — Navidrome addToPlaylist errors vary
            logger.error("SearchesPlaylist: add_to_playlist failed: %s", e)
            return False
        if not ok:
            return False

        self._store.set_playlist(transfer_id, playlist_id)
        logger.info(
            "SearchesPlaylist: added %s - %s to '%s' (%s)",
            artist,
            title,
            SEARCHES_PLAYLIST_NAME,
            playlist_id,
        )
        return True

    def retry_unplaylisted_downloads(self, resolve_intent) -> int:
        """Retry manual downloads waiting on library indexing.

        `resolve_intent(row) -> (title, artist)` is supplied by the caller
        (DownloadMonitor already has the machinery to look up a transfer's
        search row) rather than duplicated here.
        """
        linked = 0
        for row in self._store.get_unplaylisted_manual_downloads():
            title, artist = resolve_intent(row)
            if not title and not artist:
                continue
            if self.add_downloaded_to_playlist(row["id"], title, artist):
                linked += 1
        if linked:
            logger.info("SearchesPlaylist: linked %d delayed download(s)", linked)
        return linked
