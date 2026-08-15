"""
NavidromeLibrary — Concrete implementation of LibraryService using Navidrome Subsonic API.

Manages the Navidrome music library: search, ratings, playlists, scans.
"""

import hashlib
import random
import string
import time
from typing import Optional
import requests

from app.config import Config
from app.exceptions import (
    LibraryError,
    PlaylistNotFoundError,
    PlaylistError,
    NavidromeConnectionError
)
from app.logging_config import get_logger
from app.services.library import LibraryService, PlaylistDetail, PlaylistInfo, Song

logger = get_logger(__name__)


class NavidromeLibrary(LibraryService):
    """
    Navidrome-based library implementation.
    
    Uses the Subsonic API to manage the Navidrome music library.
    """
    
    def __init__(self, config: Config):
        """
        Initialize NavidromeLibrary.
        
        Args:
            config: Config object with Navidrome settings
        """
        self.config = config
        self.base_url = config.navidrome.url
        self.username = config.navidrome.username
        self.password = config.navidrome.password
        self.session = requests.Session()
        # P6.7-6: cached Navidrome native-API JWT (auth/login) for
        # get_song_real_path(). Refreshed lazily on expiry/401 — never
        # eagerly, since TrashPurge is the only consumer.
        self._native_jwt: str | None = None
        self._native_jwt_at: float = 0.0
    
    def _get_auth_params(self) -> dict:
        """Get Subsonic authentication parameters."""
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "musica-sync",
            "f": "json"
        }
    
    def _safe_json(self, resp: requests.Response, endpoint: str) -> dict:
        """Parse JSON response safely."""
        if resp.status_code != 200:
            logger.error(f"Navidrome {endpoint} returned HTTP {resp.status_code}: {resp.text[:200]}")
            return {}
        
        if not resp.text:
            logger.error(f"Navidrome {endpoint} returned empty body")
            return {}
        
        try:
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to parse Navidrome {endpoint} response: {e}")
            return {}
    
    def _subsonic_ok(self, resp: requests.Response, endpoint: str) -> bool:
        """Check if Subsonic response is successful."""
        if resp.status_code != 200:
            logger.error(f"Navidrome {endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        
        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"Navidrome {endpoint} unparseable response: {e}")
            return False
        
        sub = data.get("subsonic-response", {})
        if sub.get("status") != "ok":
            err = sub.get("error", {})
            logger.error(f"Navidrome {endpoint} Subsonic error {err.get('code')}: {err.get('message')}")
            return False
        
        return True
    
    def _parse_song(self, entry: dict) -> Song:
        """Parse a Subsonic song entry into a Song object."""
        return Song(
            song_id=entry.get("id", ""),
            title=entry.get("title", ""),
            artist=entry.get("artist", ""),
            album=entry.get("album", ""),
            path=entry.get("path", ""),
            duration=entry.get("duration", 0),
            size=entry.get("size", 0),
            bitrate=entry.get("bitRate"),
            track_number=entry.get("track"),
            year=entry.get("year"),
            genre=entry.get("genre"),
            rating=entry.get("userRating", 0),
            starred="starred" in entry,
            mbid=entry.get("musicBrainzId")
        )
    
    def search_library(self, query: str) -> list[Song]:
        """
        Search Navidrome library via search3 API.
        
        Args:
            query: Search query (artist, album, or track name)
            
        Returns:
            List of matching Song objects
        """
        logger.info(f"Searching library: {query}")
        
        params = {**self._get_auth_params(), "query": query, "songCount": 50}
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/search3",
                params=params,
                timeout=10
            )
            
            data = self._safe_json(resp, "search3")
            if not data:
                return []
            
            search_result = data.get("subsonic-response", {}).get("searchResult3", {})
            songs = search_result.get("song", [])
            
            if isinstance(songs, dict):
                songs = [songs]
            
            result = [self._parse_song(song) for song in songs]
            logger.info(f"Search returned {len(result)} results")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Search connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))
    
    def get_starred(self) -> list[Song]:
        """
        Get starred/favorited songs.
        
        Returns:
            List of starred Song objects
        """
        logger.debug("Fetching starred songs")
        
        params = self._get_auth_params()
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/getStarred",
                params=params,
                timeout=10
            )
            
            data = self._safe_json(resp, "getStarred")
            if not data:
                return []
            
            starred = data.get("subsonic-response", {}).get("starred", {})
            songs = starred.get("song", [])
            
            if isinstance(songs, dict):
                songs = [songs]
            
            result = [self._parse_song(song) for song in songs]
            logger.info(f"Found {len(result)} starred songs")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Get starred connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))
    
    def set_rating(self, song_id: str, rating: int) -> bool:
        """
        Set song rating (0-5).
        
        Args:
            song_id: Navidrome song ID
            rating: Rating value (0-5)
            
        Returns:
            True if successful
        """
        if rating < 0 or rating > 5:
            raise ValueError(f"Rating must be 0-5, got {rating}")
        
        logger.info(f"Setting rating for {song_id}: {rating}")
        
        params = {**self._get_auth_params(), "id": song_id, "rating": rating}
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/setRating",
                params=params,
                timeout=10
            )
            
            success = resp.status_code == 200
            if success:
                logger.info(f"Rating set successfully")
            else:
                logger.error(f"Failed to set rating: HTTP {resp.status_code}")
            
            return success
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Set rating connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))
    
    def create_playlist(self, name: str) -> str:
        """
        Create a new playlist.
        
        Args:
            name: Playlist name
            
        Returns:
            Playlist ID
        """
        logger.info(f"Creating playlist: {name}")
        
        params = {**self._get_auth_params(), "name": name}
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/createPlaylist",
                params=params,
                timeout=10
            )
            
            data = self._safe_json(resp, "createPlaylist")
            if not data:
                raise PlaylistError(name, "create", "Empty response")
            
            playlist_id = data.get("subsonic-response", {}).get("playlist", {}).get("id")
            if not playlist_id:
                raise PlaylistError(name, "create", "No playlist ID returned")
            
            logger.info(f"Playlist created: {playlist_id}")
            return playlist_id
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Create playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))
    
    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        """
        Rename an existing playlist via Subsonic's updatePlaylist "name" param.

        Args:
            playlist_id: Navidrome playlist ID
            name: New playlist name

        Returns:
            True if successful
        """
        logger.info(f"Renaming playlist {playlist_id} -> {name}")

        params = {**self._get_auth_params(), "playlistId": playlist_id, "name": name}

        try:
            resp = self.session.get(
                f"{self.base_url}/rest/updatePlaylist",
                params=params,
                timeout=10
            )

            success = self._subsonic_ok(resp, "updatePlaylist(rename)")
            if success:
                logger.info("Playlist renamed successfully")
            else:
                logger.error("Failed to rename playlist")

            return success

        except requests.exceptions.RequestException as e:
            logger.error(f"Rename playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    def update_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Replace playlist contents with song_ids.
        
        Note: This actually appends songs. To replace, you'd need to clear first.
        For now, we use add_to_playlist which appends.
        
        Args:
            playlist_id: Navidrome playlist ID
            song_ids: List of song IDs
            
        Returns:
            True if successful
        """
        return self.add_to_playlist(playlist_id, song_ids)
    
    def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Append songs to existing playlist.
        
        Args:
            playlist_id: Navidrome playlist ID
            song_ids: List of song IDs to add
            
        Returns:
            True if successful
        """
        if not song_ids:
            logger.warning("No song IDs provided")
            return True
        
        logger.info(f"Adding {len(song_ids)} songs to playlist {playlist_id}")
        
        params = {**self._get_auth_params(), "playlistId": playlist_id}
        # Subsonic updatePlaylist uses "songIdToAdd" (not "songId")
        params["songIdToAdd"] = song_ids
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/updatePlaylist",
                params=params,
                timeout=10
            )
            
            success = self._subsonic_ok(resp, "updatePlaylist(add)")
            if success:
                logger.info(f"Songs added successfully")
            else:
                logger.error(f"Failed to add songs")
            
            return success
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Add to playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))
    
    def remove_songs_from_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Remove songs from an existing playlist by song ID.

        Resolves each song ID to its index position(s) in the playlist's current
        track order (via get_playlist_detail), then calls Subsonic's updatePlaylist
        with songIndexToRemove. Subsonic has no "remove by song ID" primitive, so
        removal is index-based.

        Duplicate occurrences are collapsed: a song appearing multiple times is
        removed from every position except the most recently added one; a song
        appearing once is removed entirely.

        Args:
            playlist_id: Navidrome playlist ID
            song_ids: List of song IDs to remove

        Returns:
            True if successful, or if there is nothing to remove
            False if the playlist does not exist

        Raises:
            NavidromeConnectionError: If cannot connect to Navidrome
        """
        if not song_ids:
            logger.warning("No song IDs provided")
            return True

        try:
            detail = self.get_playlist_detail(playlist_id)
        except PlaylistNotFoundError:
            logger.warning(f"Playlist {playlist_id} not found, nothing to remove")
            return False

        indices_to_remove = self._resolve_remove_indices(detail.songs, song_ids)

        if not indices_to_remove:
            logger.info(f"No matching songs to remove from playlist {playlist_id}")
            return True

        logger.info(
            f"Removing {len(indices_to_remove)} songs from playlist {playlist_id} "
            f"at indices {indices_to_remove}"
        )

        # Descending order: later indices are removed first, so earlier
        # positions stay valid regardless of how Subsonic applies them.
        params = {
            **self._get_auth_params(),
            "playlistId": playlist_id,
            "songIndexToRemove": sorted(indices_to_remove, reverse=True),
        }

        try:
            resp = self.session.get(
                f"{self.base_url}/rest/updatePlaylist",
                params=params,
                timeout=10
            )

            success = self._subsonic_ok(resp, "updatePlaylist(remove)")
            if success:
                logger.info("Songs removed successfully")
            else:
                logger.error("Failed to remove songs")
            return success

        except requests.exceptions.RequestException as e:
            logger.error(f"Remove from playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    @staticmethod
    def _resolve_remove_indices(songs: list[Song], song_ids: list[str]) -> list[int]:
        """Map song IDs to playlist indices for removal.

        A song appearing multiple times in the playlist is removed from all
        positions except the most recently added one (the last occurrence).
        """
        indices_by_song: dict[str, list[int]] = {}
        for index, song in enumerate(songs):
            if song.song_id in song_ids:
                indices_by_song.setdefault(song.song_id, []).append(index)

        return [
            index
            for indices in indices_by_song.values()
            for index in (indices[:-1] if len(indices) > 1 else indices)
        ]

    def delete_playlist(self, playlist_id: str) -> bool:
        """
        Delete a playlist entirely.

        Uses Subsonic's deletePlaylist endpoint, which takes `id` (not
        `playlistId` — the wrong name returns "missing parameter: 'id'";
        live-verified 2026-08-13 on Navidrome 0.63.2).

        Args:
            playlist_id: Navidrome playlist ID

        Returns:
            True if deleted, False if Navidrome reports failure

        Raises:
            NavidromeConnectionError: If cannot connect to Navidrome
        """
        logger.info(f"Deleting playlist: {playlist_id}")

        params = {**self._get_auth_params(), "id": playlist_id}

        try:
            resp = self.session.get(
                f"{self.base_url}/rest/deletePlaylist",
                params=params,
                timeout=10,
            )

            success = self._subsonic_ok(resp, "deletePlaylist")
            if success:
                logger.info("Playlist deleted successfully")
            else:
                logger.error("Failed to delete playlist")
            return success

        except requests.exceptions.RequestException as e:
            logger.error(f"Delete playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    def get_playlist(self, playlist_id: str) -> list[Song]:
        """
        Get playlist contents.
        
        Args:
            playlist_id: Navidrome playlist ID
            
        Returns:
            List of Song objects in playlist
        """
        logger.debug(f"Fetching playlist: {playlist_id}")
        
        params = {**self._get_auth_params(), "id": playlist_id}
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/getPlaylist",
                params=params,
                timeout=10
            )
            
            data = self._safe_json(resp, "getPlaylist")
            if not data:
                raise PlaylistNotFoundError(playlist_id)
            
            playlist = data.get("subsonic-response", {}).get("playlist", {})
            entries = playlist.get("entry", [])
            
            if isinstance(entries, dict):
                entries = [entries]
            
            result = [self._parse_song(entry) for entry in entries]
            logger.info(f"Playlist has {len(result)} songs")
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Get playlist connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    def list_playlists(self) -> list[PlaylistInfo]:
        """
        List all Navidrome playlists.

        Returns:
            List of PlaylistInfo objects
        """
        logger.info("Listing playlists")

        params = self._get_auth_params()

        try:
            resp = self.session.get(
                f"{self.base_url}/rest/getPlaylists",
                params=params,
                timeout=10,
            )

            data = self._safe_json(resp, "getPlaylists")
            if not data:
                return []

            playlists_data = (
                data.get("subsonic-response", {})
                .get("playlists", {})
                .get("playlist", [])
            )

            if isinstance(playlists_data, dict):
                playlists_data = [playlists_data]

            result = [
                PlaylistInfo(
                    playlist_id=p.get("id", ""),
                    name=p.get("name", ""),
                    song_count=p.get("songCount", 0),
                    duration=p.get("duration", 0),
                    public=p.get("public", False),
                    owner=p.get("owner"),
                    comment=p.get("comment"),
                    created=p.get("created"),
                    changed=p.get("changed"),
                )
                for p in playlists_data
            ]
            logger.info(f"Found {len(result)} playlists")
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"List playlists connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    def get_playlist_detail(self, playlist_id: str) -> PlaylistDetail:
        """
        Get playlist contents with metadata.

        Args:
            playlist_id: Navidrome playlist ID

        Returns:
            PlaylistDetail with name and songs

        Raises:
            PlaylistNotFoundError: If playlist_id not found
            NavidromeConnectionError: If cannot connect to Navidrome
        """
        logger.debug(f"Fetching playlist detail: {playlist_id}")

        params = {**self._get_auth_params(), "id": playlist_id}

        try:
            resp = self.session.get(
                f"{self.base_url}/rest/getPlaylist",
                params=params,
                timeout=10,
            )

            data = self._safe_json(resp, "getPlaylist")
            if not data:
                raise PlaylistNotFoundError(playlist_id)

            sub = data.get("subsonic-response", {})
            if sub.get("status") != "ok":
                # Navidrome replies HTTP 200 with status="failed" for unknown ids
                raise PlaylistNotFoundError(playlist_id)

            playlist = data.get("subsonic-response", {}).get("playlist", {})
            name = playlist.get("name", "")
            entries = playlist.get("entry", [])

            if isinstance(entries, dict):
                entries = [entries]

            songs = [self._parse_song(entry) for entry in entries]
            logger.info(f"Playlist '{name}' has {len(songs)} songs")
            return PlaylistDetail(playlist_id=playlist_id, name=name, songs=songs)

        except requests.exceptions.RequestException as e:
            logger.error(f"Get playlist detail connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    def trigger_scan(self) -> bool:
        """
        Trigger Navidrome library scan.
        
        Returns:
            True if scan triggered successfully
        """
        logger.info("Triggering library scan")
        
        params = self._get_auth_params()
        
        try:
            resp = self.session.get(
                f"{self.base_url}/rest/startScan",
                params=params,
                timeout=10
            )
            
            success = resp.status_code == 200
            if success:
                logger.info(f"Scan triggered successfully")
            else:
                logger.error(f"Failed to trigger scan: HTTP {resp.status_code}")
            
            return success
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Trigger scan connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

    # ------------------------------------------------------------------
    # P6.7-6: real filesystem paths via the native API
    #
    # The Subsonic `path` field is synthesized from tags (see
    # tests/live/probes/navidrome.py — verified on 0.63.2), so it cannot
    # locate a file on disk. Navidrome's own API returns the real path:
    # POST /auth/login -> JWT, then GET /api/song/{id} with
    # `X-ND-Authorization: Bearer <jwt>` gives `path` relative to
    # `libraryPath`. Both endpoints were live-verified 2026-08-13.
    # ------------------------------------------------------------------

    def _native_token(self) -> str | None:
        """Return a cached Navidrome native-API JWT, logging in if needed.

        The JWT lives ~48h (live-verified: exp - iat = 172800s), so the
        cache is only refreshed when it's missing, past an hour of age, or
        a call with it came back unauthenticated. Returns None when
        Navidrome refuses the login.
        """
        if self._native_jwt is not None and time.time() - self._native_jwt_at < 3600:
            return self._native_jwt
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Navidrome auth/login connection error: {e}")
            return None
        if resp.status_code != 200:
            logger.error(
                f"Navidrome auth/login returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return None
        try:
            token = resp.json().get("token")
        except ValueError:
            logger.error("Navidrome auth/login returned unparseable JSON")
            return None
        if not token:
            logger.error("Navidrome auth/login returned no token")
            return None
        self._native_jwt = token
        self._native_jwt_at = time.time()
        return token

    def get_song_real_path(self, song_id: str) -> str | None:
        """
        Resolve a song's real filesystem path (relative to music_dir).

        Uses Navidrome's native API — Subsonic's `path` field is
        tag-synthesized and unusable for file operations. Returns the
        relative path (e.g. `discovery/Comfort_Zone/.../07 Alright.flac`), or
        None when the song is unknown or Navidrome can't be reached.

        Raises:
            NavidromeConnectionError: If cannot connect to Navidrome
        """
        token = self._native_token()
        if token is None:
            return None

        headers = {"X-ND-Authorization": f"Bearer {token}"}
        try:
            resp = self.session.get(
                f"{self.base_url}/api/song/{song_id}",
                headers=headers,
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Navidrome song lookup connection error: {e}")
            raise NavidromeConnectionError(self.base_url, str(e))

        if resp.status_code == 401:
            # Stale or rejected token — drop the cache and try once more.
            self._native_jwt = None
            token = self._native_token()
            if token is None:
                return None
            resp = self.session.get(
                f"{self.base_url}/api/song/{song_id}",
                headers={"X-ND-Authorization": f"Bearer {token}"},
                timeout=10,
            )

        if resp.status_code != 200:
            logger.error(
                f"Navidrome song lookup returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.error(f"Navidrome song lookup unparseable response: {resp.text[:200]}")
            return None

        path = (data or {}).get("path")
        if not path:
            logger.warning(f"No path for Navidrome song {song_id}")
            return None
        logger.debug("Navidrome song %s real path: %s", song_id, path)
        return str(path)

    def link_listenbrainz(self, token: str) -> bool:
        """Enable ListenBrainz scrobbling for the admin user by calling
        Navidrome's own PUT /api/listenbrainz/link.

        There is no env var / config.toml equivalent — confirmed live
        2026-08-14 by watching Navidrome's own web UI make this exact call
        when a user pastes a token into its "Scrobble to ListenBrainz"
        toggle. ND_LISTENBRAINZ_TOKEN in docker-compose.yml is not a real
        Navidrome config key and has always been a silent no-op; scrobbling
        was never actually enabled by it. Navidrome validates the token
        live against ListenBrainz's own API before accepting it, so a
        False return here can mean "unreachable" just as easily as "bad
        token" — check logs for which.
        """
        auth_token = self._native_token()
        if auth_token is None:
            return False
        try:
            resp = self.session.put(
                f"{self.base_url}/api/listenbrainz/link",
                json={"token": token},
                headers={"X-ND-Authorization": f"Bearer {auth_token}"},
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            logger.warning(f"Navidrome ListenBrainz link connection error: {e}")
            return False
        if resp.status_code != 200:
            logger.warning(
                f"Navidrome ListenBrainz link failed: HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
            return False
        logger.info("Navidrome ListenBrainz scrobbling linked for admin user")
        return True
