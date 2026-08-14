"""
RecPlaylistService — gets a completed rec download into its category playlist.

Shared by DownloadMonitor (the P6.7-7 add-on-completion hook: the moment a
rec's file lands in the library, the track should appear in the user's
playlist) and RecPuller (a per-pull retry pass for completions that missed
the hook — Navidrome's index can lag the beets import, and the S12 live
audit treats a downloaded rec absent from its playlist as a failure).

The playlist is found by the category's configured name and created lazily
if missing, exactly like the puller's own `_ensure_playlist` — a playlist
deleted by the user stays deleted until there is a track to add.
"""

from app.logging_config import get_logger
from app.services.recommendation import normalize_text

logger = get_logger(__name__)

CATEGORIES = ("comfort_zone", "fresh_picks", "deep_cuts")


class RecPlaylistService:
    """Adds downloaded recommendation tracks to their category playlist."""

    def __init__(self, config, library_service, recs_store):
        """
        Initialize RecPlaylistService.

        Args:
            config: Config instance (recs playlist names)
            library_service: LibraryService implementation
            recs_store: RecsStore instance
        """
        self._config = config
        self._library = library_service
        self._store = recs_store

    @staticmethod
    def _rec_key(artist: str, track: str) -> str:
        return f"{normalize_text(artist)}::{normalize_text(track)}"

    def _find_or_create_playlist(self, name: str) -> str | None:
        """Find a playlist by name, creating it if needed. None on failure."""
        try:
            existing = self._library.list_playlists()
        except Exception:  # noqa: BLE001 — library backends vary
            logger.warning("RecPlaylist: list_playlists failed, assuming none")
            existing = []
        match = next((p for p in existing if p.name.lower() == name.lower()), None)
        if match is not None:
            return match.playlist_id
        try:
            playlist_id = self._library.create_playlist(name)
            logger.info("RecPlaylist: created playlist '%s' -> %s", name, playlist_id)
            return playlist_id
        except Exception as e:  # noqa: BLE001 — Navidrome createPlaylist errors vary
            logger.error("RecPlaylist: create_playlist failed for '%s': %s", name, e)
            return None

    def _find_song(self, rec_row: dict):
        """Find the rec's track in the library after beets normalization.

        Fresh Picks rows often have no recording MBID, and beets may replace
        a peer's title with MusicBrainz metadata during import. Search by the
        track first, then the artist, and match the requested title against
        either the indexed title or album before accepting a unique artist
        result as the final fallback.
        """
        track = rec_row.get("track") or ""
        artist = rec_row.get("artist") or ""
        queries = [track]
        if artist and normalize_text(artist) != normalize_text(track):
            queries.append(artist)

        songs = []
        seen_ids: set[str] = set()
        for query in queries:
            try:
                matches = self._library.search_library(query)
            except Exception:  # noqa: BLE001 — library backends vary
                logger.warning(
                    "RecPlaylist: library probe failed for %s - %s",
                    artist,
                    track,
                )
                continue
            for song in matches:
                if song.song_id and song.song_id not in seen_ids:
                    songs.append(song)
                    seen_ids.add(song.song_id)

        key = self._rec_key(rec_row.get("artist") or "", rec_row.get("track") or "")
        for song in songs:
            if song.song_id and self._rec_key(song.artist, song.title) == key:
                return song

        target_artist = normalize_text(artist)
        target_track = normalize_text(track)
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
        for song in artist_matches:
            indexed_names = (
                normalize_text(song.title),
                normalize_text(song.album),
            )
            if target_track and any(
                name == target_track or name in target_track or target_track in name
                for name in indexed_names
                if name
            ):
                return song
        if len(artist_matches) == 1:
            return artist_matches[0]
        return None

    def add_downloaded_to_playlist(self, rec_row: dict) -> bool:
        """
        Add a completed rec download's track to its category playlist.

        Returns True when the track is in the playlist (added now, or
        already there); False when there is nothing to add yet — the file
        may not be indexed, the playlist could not be found/created, or the
        rec has no resolvable category (no fallback, P6.7-0b). On success
        the rec row's playlist_id is recorded (the S12 linkage).
        """
        source = rec_row.get("source")
        if source not in CATEGORIES:
            logger.warning(
                "RecPlaylist: rec %s has unknown source %r; no playlist "
                "to add it to (no fallback)",
                rec_row.get("id"),
                source,
            )
            return False

        name = getattr(self._config.recs, f"{source}_playlist_name")
        playlist_id = self._find_or_create_playlist(name)
        if playlist_id is None:
            return False

        song = self._find_song(rec_row)
        if song is None:
            logger.info(
                "RecPlaylist: %s - %s not found in library yet (index lag?), "
                "will retry on the next pull",
                rec_row.get("artist"),
                rec_row.get("track"),
            )
            return False

        try:
            detail = self._library.get_playlist_detail(playlist_id)
            already = {s.song_id for s in detail.songs if s.song_id}
        except Exception:  # noqa: BLE001 — playlist backends vary
            logger.warning(
                "RecPlaylist: get_playlist_detail failed for %s", playlist_id
            )
            return False
        if song.song_id in already:
            self._store.set_playlist(rec_row["id"], playlist_id)
            return True

        try:
            ok = self._library.add_to_playlist(playlist_id, [song.song_id])
        except Exception as e:  # noqa: BLE001 — Navidrome addToPlaylist errors vary
            logger.error("RecPlaylist: add_to_playlist failed: %s", e)
            return False
        if not ok:
            return False

        self._store.set_playlist(rec_row["id"], playlist_id)
        logger.info(
            "RecPlaylist: added %s - %s to playlist '%s' (%s)",
            rec_row.get("artist"),
            rec_row.get("track"),
            name,
            playlist_id,
        )
        return True

    def retry_unplaylisted_downloads(
        self, sources: tuple[str, ...] | None = None
    ) -> int:
        """Retry downloaded recs that are waiting for library indexing.

        Beets can finish before Navidrome's asynchronous scan exposes the
        song. The monitor calls this on later polls, while the puller can
        restrict it to categories participating in the current pull.
        """
        linked = 0
        categories = CATEGORIES if sources is None else sources
        for source in categories:
            for row in self._store.get_unplaylisted_downloaded(source):
                if self.add_downloaded_to_playlist(row):
                    linked += 1
        if linked:
            logger.info("RecPlaylist: linked %d delayed download(s)", linked)
        return linked
