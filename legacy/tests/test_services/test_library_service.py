"""
Unit tests for LibraryService.
"""

import pytest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import Mock, MagicMock

from app.services.library import LibraryService, Song


class MockConfig:
    """Mock config for testing."""
    NAVIDROME_URL = "http://navidrome:4533"
    NAVIDROME_USERNAME = "testuser"
    NAVIDROME_PASSWORD = "testpass"


class TestLibraryService:
    """Test LibraryService methods."""
    
    def test_init(self):
        """LibraryService should initialize with config."""
        config = MockConfig()
        library = LibraryService(config)
        
        assert library.config == config
        assert library.base_url == "http://navidrome:4533"
        assert library.username == "testuser"
        assert library.password == "testpass"
    
    def test_search_library_returns_list(self):
        """search_library() should return list of Song objects."""
        config = MockConfig()
        library = LibraryService(config)
        results = library.search_library("Bohemian Rhapsody")
        
        assert isinstance(results, list)
        # Currently returns empty list (placeholder)
        assert len(results) == 0
    
    def test_get_starred_returns_list(self):
        """get_starred() should return list of Song objects."""
        config = MockConfig()
        library = LibraryService(config)
        results = library.get_starred()
        
        assert isinstance(results, list)
        assert len(results) == 0
    
    def test_set_rating_valid(self):
        """set_rating() should accept valid ratings (0-5)."""
        config = MockConfig()
        library = LibraryService(config)
        
        for rating in range(6):  # 0-5
            result = library.set_rating("song-123", rating)
            assert result is True
    
    def test_set_rating_invalid_low(self):
        """set_rating() should reject rating < 0."""
        config = MockConfig()
        library = LibraryService(config)
        
        with pytest.raises(ValueError, match="Rating must be 0-5"):
            library.set_rating("song-123", -1)
    
    def test_set_rating_invalid_high(self):
        """set_rating() should reject rating > 5."""
        config = MockConfig()
        library = LibraryService(config)
        
        with pytest.raises(ValueError, match="Rating must be 0-5"):
            library.set_rating("song-123", 6)
    
    def test_create_playlist_returns_id(self):
        """create_playlist() should return playlist ID."""
        config = MockConfig()
        library = LibraryService(config)
        playlist_id = library.create_playlist("My Playlist")
        
        assert isinstance(playlist_id, str)
        assert len(playlist_id) > 0
    
    def test_update_playlist_returns_bool(self):
        """update_playlist() should return True on success."""
        config = MockConfig()
        library = LibraryService(config)
        result = library.update_playlist("playlist-123", ["song-1", "song-2"])
        
        assert result is True
    
    def test_add_to_playlist_returns_bool(self):
        """add_to_playlist() should return True on success."""
        config = MockConfig()
        library = LibraryService(config)
        result = library.add_to_playlist("playlist-123", ["song-3", "song-4"])
        
        assert result is True

    def test_remove_songs_from_playlist_returns_bool(self):
        """remove_songs_from_playlist() should return True on success."""
        config = MockConfig()
        library = LibraryService(config)
        result = library.remove_songs_from_playlist("playlist-123", ["song-1", "song-2"])

        assert result is True

    def test_delete_playlist_returns_bool(self):
        """delete_playlist() should return True on success."""
        config = MockConfig()
        library = LibraryService(config)
        result = library.delete_playlist("playlist-123")

        assert result is True
    
    def test_get_playlist_returns_list(self):
        """get_playlist() should return list of Song objects."""
        config = MockConfig()
        library = LibraryService(config)
        results = library.get_playlist("playlist-123")
        
        assert isinstance(results, list)
        assert len(results) == 0
    
    def test_trigger_scan_returns_bool(self):
        """trigger_scan() should return True on success."""
        config = MockConfig()
        library = LibraryService(config)
        result = library.trigger_scan()
        
        assert result is True


class TestSongDataclass:
    """Test Song dataclass."""
    
    def test_song_creation_full(self):
        """Song should be creatable with all fields."""
        song = Song(
            song_id="song-123",
            title="Bohemian Rhapsody",
            artist="Queen",
            album="A Night at the Opera",
            path="/music/Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
            duration=354,
            size=8493600,
            bitrate=192,
            track_number=1,
            year=1975,
            genre="Rock",
            rating=5,
            starred=True,
            mbid="612400e0-0c14-4f31-8e45-c98c8641b664"
        )
        
        assert song.song_id == "song-123"
        assert song.title == "Bohemian Rhapsody"
        assert song.artist == "Queen"
        assert song.album == "A Night at the Opera"
        assert song.path == "/music/Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3"
        assert song.duration == 354
        assert song.size == 8493600
        assert song.bitrate == 192
        assert song.track_number == 1
        assert song.year == 1975
        assert song.genre == "Rock"
        assert song.rating == 5
        assert song.starred is True
        assert song.mbid == "612400e0-0c14-4f31-8e45-c98c8641b664"
    
    def test_song_creation_minimal(self):
        """Song should be creatable with required fields only."""
        song = Song(
            song_id="song-123",
            title="Test Song",
            artist="Test Artist",
            album="Test Album",
            path="/music/test.mp3",
            duration=180,
            size=4320000,
            bitrate=128,
            track_number=None,
            year=None,
            genre=None,
            rating=0,
            starred=False
        )
        
        assert song.song_id == "song-123"
        assert song.title == "Test Song"
        assert song.track_number is None
        assert song.year is None
        assert song.genre is None
        assert song.mbid is None
    
    def test_song_with_mbid(self):
        """Song should support MusicBrainz ID."""
        song = Song(
            song_id="song-123",
            title="Test",
            artist="Artist",
            album="Album",
            path="/music/test.mp3",
            duration=180,
            size=4320000,
            bitrate=128,
            track_number=1,
            year=2020,
            genre="Pop",
            rating=3,
            starred=False,
            mbid="abc123-def456"
        )
        
        assert song.mbid == "abc123-def456"
    
    def test_song_without_mbid(self):
        """Song should allow None for mbid."""
        song = Song(
            song_id="song-123",
            title="Test",
            artist="Artist",
            album="Album",
            path="/music/test.mp3",
            duration=180,
            size=4320000,
            bitrate=128,
            track_number=1,
            year=2020,
            genre="Pop",
            rating=3,
            starred=False
        )
        
        assert song.mbid is None


class TestLibraryServiceIntegration:
    """Integration-style tests for LibraryService workflows."""
    
    def test_search_and_rate_workflow(self):
        """Test workflow: search → rate → verify."""
        config = MockConfig()
        library = LibraryService(config)
        
        # Search
        results = library.search_library("Queen")
        assert isinstance(results, list)
        
        # Rate (if we had results)
        if results:
            song_id = results[0].song_id
            library.set_rating(song_id, 5)
    
    def test_playlist_workflow(self):
        """Test workflow: create playlist → add songs → get playlist."""
        config = MockConfig()
        library = LibraryService(config)
        
        # Create playlist
        playlist_id = library.create_playlist("My Favorites")
        assert isinstance(playlist_id, str)
        
        # Add songs
        song_ids = ["song-1", "song-2", "song-3"]
        result = library.add_to_playlist(playlist_id, song_ids)
        assert result is True
        
        # Get playlist
        songs = library.get_playlist(playlist_id)
        assert isinstance(songs, list)
    
    def test_starred_and_scan_workflow(self):
        """Test workflow: get starred → trigger scan."""
        config = MockConfig()
        library = LibraryService(config)
        
        # Get starred
        starred = library.get_starred()
        assert isinstance(starred, list)
        
        # Trigger scan
        result = library.trigger_scan()
        assert result is True
