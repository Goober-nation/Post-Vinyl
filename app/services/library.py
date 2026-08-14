"""
LibraryService — Navidrome-specific library management.

This is a concrete class (not abstract) because Navidrome is the only
supported library backend. No plans to support Plex/Jellyfin.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Song:
    """Represents a song in the Navidrome library."""
    song_id: str
    title: str
    artist: str
    album: str
    path: str
    duration: int  # seconds
    size: int  # bytes
    bitrate: Optional[int]  # kbps
    track_number: Optional[int]
    year: Optional[int]
    genre: Optional[str]
    rating: int  # 0-5
    starred: bool
    mbid: Optional[str] = None  # MusicBrainz recording ID


@dataclass
class PlaylistInfo:
    """Summary of a Navidrome playlist."""

    playlist_id: str
    name: str
    song_count: int
    duration: int = 0  # seconds
    public: bool = False
    owner: str | None = None
    comment: str | None = None
    created: str | None = None  # ISO string
    changed: str | None = None  # ISO string


@dataclass
class PlaylistDetail:
    """Playlist contents from Navidrome."""

    playlist_id: str
    name: str
    songs: list[Song]


class LibraryService:
    """
    Navidrome-specific library management.
    
    This is a concrete class because Navidrome is the only supported backend.
    All methods interact with the Navidrome Subsonic API.
    
    Usage:
        library = LibraryService(config)
        songs = library.search_library("Bohemian Rhapsody")
        library.set_rating(song_id, 5)
        playlist_id = library.create_playlist("My Playlist")
    """
    
    def __init__(self, config):
        """
        Initialize LibraryService.
        
        Args:
            config: Config object with Navidrome URL, username, password
        """
        self.config = config
        self.base_url = config.NAVIDROME_URL
        self.username = config.NAVIDROME_USERNAME
        self.password = config.NAVIDROME_PASSWORD
        # In real implementation: initialize session, auth token, etc.
    
    def search_library(self, query: str) -> list[Song]:
        """
        Search Navidrome library via search3 API.
        
        Args:
            query: Search query (artist, album, or track name)
            
        Returns:
            List of matching Song objects
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            LibraryError: If search fails
        """
        # In real implementation: call Navidrome search3 API
        # For now: return empty list (placeholder)
        return []
    
    def get_starred(self) -> list[Song]:
        """
        Get starred/favorited songs.
        
        Returns:
            List of starred Song objects
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
        """
        # In real implementation: call Navidrome getStarred API
        return []
    
    def set_rating(self, song_id: str, rating: int) -> bool:
        """
        Set song rating (0-5).
        
        Args:
            song_id: Navidrome song ID
            rating: Rating value (0-5, where 0 means remove rating)
            
        Returns:
            True if successful, False otherwise
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            ValueError: If rating not in 0-5 range
        """
        if rating < 0 or rating > 5:
            raise ValueError(f"Rating must be 0-5, got {rating}")
        
        # In real implementation: call Navidrome setRating API
        return True
    
    def create_playlist(self, name: str) -> str:
        """
        Create a new playlist.
        
        Args:
            name: Playlist name
            
        Returns:
            Playlist ID
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            PlaylistError: If creation fails
        """
        # In real implementation: call Navidrome createPlaylist API
        # Return placeholder ID
        return "playlist-123"
    
    def update_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Replace playlist contents with song_ids.
        
        Args:
            playlist_id: Navidrome playlist ID
            song_ids: List of song IDs to set as playlist contents
            
        Returns:
            True if successful, False otherwise
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            PlaylistNotFoundError: If playlist_id not found
        """
        # In real implementation: call Navidrome updatePlaylist API
        # Note: Uses songIdToAdd parameter (not songId)
        return True
    
    def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Append songs to existing playlist.
        
        Args:
            playlist_id: Navidrome playlist ID
            song_ids: List of song IDs to add
            
        Returns:
            True if successful, False otherwise
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            PlaylistNotFoundError: If playlist_id not found
        """
        # In real implementation: call Navidrome updatePlaylist API with songIdToAdd
        return True
    
    def remove_songs_from_playlist(self, playlist_id: str, song_ids: list[str]) -> bool:
        """
        Remove songs from an existing playlist.

        Resolves each song ID to its index position(s) in the playlist's current
        track order, then removes them via Subsonic's updatePlaylist
        songIndexToRemove. Index-based, not ID-based — Subsonic has no
        "remove by song ID" primitive.

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
            ServiceConnectionError: If cannot connect to Navidrome
        """
        return True

    def delete_playlist(self, playlist_id: str) -> bool:
        """
        Delete a playlist entirely.

        Args:
            playlist_id: Navidrome playlist ID

        Returns:
            True if deleted, False if the playlist could not be deleted

        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
        """
        return True

    def get_playlist(self, playlist_id: str) -> list[Song]:
        """
        Get playlist contents.
        
        Args:
            playlist_id: Navidrome playlist ID
            
        Returns:
            List of Song objects in playlist
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
            PlaylistNotFoundError: If playlist_id not found
        """
        # In real implementation: call Navidrome getPlaylist API
        return []
    
    def list_playlists(self) -> list[PlaylistInfo]:
        """
        List all Navidrome playlists.

        Returns:
            List of PlaylistInfo objects
        """
        return []

    def get_playlist_detail(self, playlist_id: str) -> PlaylistDetail:
        """
        Get playlist contents with metadata.

        Args:
            playlist_id: Navidrome playlist ID

        Returns:
            PlaylistDetail with name and songs

        Raises:
            PlaylistNotFoundError: If playlist_id not found
        """
        return PlaylistDetail(playlist_id=playlist_id, name="", songs=[])

    def trigger_scan(self) -> bool:
        """
        Trigger Navidrome library scan.
        
        Returns:
            True if scan triggered successfully, False otherwise
            
        Raises:
            ServiceConnectionError: If cannot connect to Navidrome
        """
        # In real implementation: call Navidrome startScan API
        return True
