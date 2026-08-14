"""
Unit tests for NavidromeLibrary implementation.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from app.services.navidrome_library import NavidromeLibrary
from app.services.library import PlaylistInfo, PlaylistDetail, Song
from app.exceptions import (
    PlaylistNotFoundError,
    PlaylistError,
    NavidromeConnectionError
)
from app.config import Config


class MockConfig:
    """Mock config for testing."""
    
    class NavidromeConfig:
        url = "http://navidrome:4533"
        username = "testuser"
        password = "testpass"
    
    navidrome = NavidromeConfig()


class TestNavidromeLibraryInit:
    """Test NavidromeLibrary initialization."""
    
    def test_init_with_config(self):
        """NavidromeLibrary should initialize with config."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        assert library.base_url == "http://navidrome:4533"
        assert library.username == "testuser"
        assert library.password == "testpass"
    
    def test_get_auth_params(self):
        """_get_auth_params should return correct auth parameters."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        params = library._get_auth_params()
        
        assert params["u"] == "testuser"
        assert "t" in params  # token
        assert "s" in params  # salt
        assert params["v"] == "1.16.1"
        assert params["c"] == "musica-sync"
        assert params["f"] == "json"


class TestNavidromeSearchLibrary:
    """Test search_library() method."""
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_search_library_success(self, mock_get):
        """search_library() should return list of Song objects."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "searchResult3": {
                    "song": [
                        {
                            "id": "song-1",
                            "title": "Bohemian Rhapsody",
                            "artist": "Queen",
                            "album": "A Night at the Opera",
                            "path": "Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
                            "duration": 354,
                            "size": 8493600,
                            "bitRate": 192,
                            "track": 1,
                            "year": 1975,
                            "genre": "Rock",
                            "userRating": 5,
                            "starred": "2024-01-01T00:00:00"
                        }
                    ]
                }
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        results = library.search_library("Queen")
        
        assert len(results) == 1
        assert isinstance(results[0], Song)
        assert results[0].title == "Bohemian Rhapsody"
        assert results[0].artist == "Queen"
        assert results[0].rating == 5
        assert results[0].starred is True
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_search_library_empty(self, mock_get):
        """search_library() should return empty list for no results."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "searchResult3": {}
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        results = library.search_library("nonexistent")
        
        assert len(results) == 0
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_search_library_connection_error(self, mock_get):
        """search_library() should raise NavidromeConnectionError on connection error."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        with pytest.raises(NavidromeConnectionError):
            library.search_library("test")


class TestNavidromeGetStarred:
    """Test get_starred() method."""
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_starred_success(self, mock_get):
        """get_starred() should return list of starred Song objects."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "starred": {
                    "song": [
                        {
                            "id": "song-1",
                            "title": "Song 1",
                            "artist": "Artist 1",
                            "album": "Album 1",
                            "path": "Artist 1/Album 1/01 - Song 1.mp3",
                            "duration": 180,
                            "size": 4320000,
                            "userRating": 5,
                            "starred": "2024-01-01T00:00:00"
                        },
                        {
                            "id": "song-2",
                            "title": "Song 2",
                            "artist": "Artist 2",
                            "album": "Album 2",
                            "path": "Artist 2/Album 2/01 - Song 2.mp3",
                            "duration": 200,
                            "size": 4800000,
                            "userRating": 5,
                            "starred": "2024-01-02T00:00:00"
                        }
                    ]
                }
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        results = library.get_starred()
        
        assert len(results) == 2
        assert all(song.starred for song in results)
        assert all(song.rating == 5 for song in results)
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_starred_empty(self, mock_get):
        """get_starred() should return empty list when no starred songs."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "starred": {}
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        results = library.get_starred()
        
        assert len(results) == 0


class TestNavidromeSetRating:
    """Test set_rating() method."""
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_set_rating_success(self, mock_get):
        """set_rating() should return True on success."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        result = library.set_rating("song-1", 5)
        
        assert result is True
    
    def test_set_rating_invalid_low(self):
        """set_rating() should raise ValueError for rating < 0."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        with pytest.raises(ValueError, match="Rating must be 0-5"):
            library.set_rating("song-1", -1)
    
    def test_set_rating_invalid_high(self):
        """set_rating() should raise ValueError for rating > 5."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        with pytest.raises(ValueError, match="Rating must be 0-5"):
            library.set_rating("song-1", 6)


class TestNavidromePlaylistOperations:
    """Test playlist operations."""
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_create_playlist_success(self, mock_get):
        """create_playlist() should return playlist ID."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlist": {"id": "playlist-123"}
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        playlist_id = library.create_playlist("My Playlist")
        
        assert playlist_id == "playlist-123"
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_create_playlist_failure(self, mock_get):
        """create_playlist() should raise PlaylistError on failure."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # Empty response
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        with pytest.raises(PlaylistError):
            library.create_playlist("My Playlist")
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_add_to_playlist_success(self, mock_get):
        """add_to_playlist() should return True on success."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {"status": "ok"}
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        result = library.add_to_playlist("playlist-123", ["song-1", "song-2"])
        
        assert result is True
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_add_to_playlist_empty_list(self, mock_get):
        """add_to_playlist() should return True for empty song list."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        result = library.add_to_playlist("playlist-123", [])
        
        assert result is True
        mock_get.assert_not_called()
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_success(self, mock_get):
        """get_playlist() should return list of Song objects."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlist": {
                    "id": "playlist-123",
                    "name": "My Playlist",
                    "entry": [
                        {
                            "id": "song-1",
                            "title": "Song 1",
                            "artist": "Artist 1",
                            "album": "Album 1",
                            "path": "path1.mp3",
                            "duration": 180,
                            "size": 4320000
                        }
                    ]
                }
            }
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        results = library.get_playlist("playlist-123")
        
        assert len(results) == 1
        assert results[0].title == "Song 1"
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_not_found(self, mock_get):
        """get_playlist() should raise PlaylistNotFoundError for empty response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # Empty response
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        with pytest.raises(PlaylistNotFoundError):
            library.get_playlist("nonexistent")


class TestNavidromeTriggerScan:
    """Test trigger_scan() method."""
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_trigger_scan_success(self, mock_get):
        """trigger_scan() should return True on success."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        result = library.trigger_scan()
        
        assert result is True
    
    @patch('app.services.navidrome_library.requests.Session.get')
    def test_trigger_scan_failure(self, mock_get):
        """trigger_scan() should return False on failure."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response
        
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        result = library.trigger_scan()
        
        assert result is False


class TestNavidromeParseSong:
    """Test _parse_song() method."""
    
    def test_parse_song_full(self):
        """_parse_song() should parse all fields correctly."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        entry = {
            "id": "song-123",
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "path": "Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
            "duration": 354,
            "size": 8493600,
            "bitRate": 192,
            "track": 1,
            "year": 1975,
            "genre": "Rock",
            "userRating": 5,
            "starred": "2024-01-01T00:00:00",
            "musicBrainzId": "612400e0-0c14-4f31-8e45-c98c8641b664"
        }
        
        song = library._parse_song(entry)
        
        assert song.song_id == "song-123"
        assert song.title == "Bohemian Rhapsody"
        assert song.artist == "Queen"
        assert song.album == "A Night at the Opera"
        assert song.duration == 354
        assert song.size == 8493600
        assert song.bitrate == 192
        assert song.track_number == 1
        assert song.year == 1975
        assert song.genre == "Rock"
        assert song.rating == 5
        assert song.starred is True
        assert song.mbid == "612400e0-0c14-4f31-8e45-c98c8641b664"
    
    def test_parse_song_minimal(self):
        """_parse_song() should handle minimal fields."""
        config = MockConfig()
        library = NavidromeLibrary(config)
        
        entry = {
            "id": "song-123",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "path": "path.mp3",
            "duration": 180,
            "size": 4320000
        }
        
        song = library._parse_song(entry)
        
        assert song.song_id == "song-123"
        assert song.title == "Song"
        assert song.bitrate is None
        assert song.track_number is None
        assert song.year is None
        assert song.genre is None
        assert song.rating == 0
        assert song.starred is False
        assert song.mbid is None


class TestNavidromeListPlaylists:
    """Test list_playlists() method."""

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_list_playlists_empty(self, mock_get):
        """list_playlists() should return empty list when no playlists."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlists": {}
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        results = library.list_playlists()

        assert len(results) == 0

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_list_playlists_with_playlists(self, mock_get):
        """list_playlists() should return PlaylistInfo objects with all fields."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlists": {
                    "playlist": [
                        {
                            "id": "pl-1",
                            "name": "Rock Classics",
                            "songCount": 42,
                            "duration": 3600,
                            "public": True,
                            "owner": "user1",
                            "comment": "Best rock songs",
                            "created": "2024-01-01T00:00:00Z",
                            "changed": "2024-06-15T12:30:00Z",
                        },
                        {
                            "id": "pl-2",
                            "name": "Chill Vibes",
                            "songCount": 15,
                            "duration": 900,
                            "public": False,
                            "owner": "user2",
                            "comment": None,
                            "created": "2024-03-01T00:00:00Z",
                            "changed": "2024-06-01T00:00:00Z",
                        },
                    ]
                }
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        results = library.list_playlists()

        assert len(results) == 2
        assert isinstance(results[0], PlaylistInfo)
        assert results[0].playlist_id == "pl-1"
        assert results[0].name == "Rock Classics"
        assert results[0].song_count == 42
        assert results[0].duration == 3600
        assert results[0].public is True
        assert results[0].owner == "user1"
        assert results[0].comment == "Best rock songs"
        assert results[0].created == "2024-01-01T00:00:00Z"
        assert results[0].changed == "2024-06-15T12:30:00Z"
        assert results[1].playlist_id == "pl-2"
        assert results[1].name == "Chill Vibes"
        assert results[1].public is False
        assert results[1].comment is None

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_list_playlists_dict_single(self, mock_get):
        """list_playlists() should handle single playlist returned as dict."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlists": {
                    "playlist": {
                        "id": "pl-single",
                        "name": "Only One",
                        "songCount": 10,
                        "duration": 600,
                        "public": True,
                        "owner": None,
                        "comment": None,
                        "created": None,
                        "changed": None,
                    }
                }
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        results = library.list_playlists()

        assert len(results) == 1
        assert results[0].playlist_id == "pl-single"
        assert results[0].name == "Only One"

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_list_playlists_connection_error(self, mock_get):
        """list_playlists() should raise NavidromeConnectionError on connection error."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(NavidromeConnectionError):
            library.list_playlists()


class TestNavidromeGetPlaylistDetail:
    """Test get_playlist_detail() method."""

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_detail_success(self, mock_get):
        """get_playlist_detail() should return PlaylistDetail with name and songs."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlist": {
                    "id": "pl-1",
                    "name": "My Playlist",
                    "entry": [
                        {
                            "id": "song-1",
                            "title": "Song One",
                            "artist": "Artist One",
                            "album": "Album One",
                            "path": "path1.mp3",
                            "duration": 200,
                            "size": 4800000,
                        },
                        {
                            "id": "song-2",
                            "title": "Song Two",
                            "artist": "Artist Two",
                            "album": "Album Two",
                            "path": "path2.mp3",
                            "duration": 180,
                            "size": 4320000,
                        },
                    ],
                }
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.get_playlist_detail("pl-1")

        assert isinstance(result, PlaylistDetail)
        assert result.playlist_id == "pl-1"
        assert result.name == "My Playlist"
        assert len(result.songs) == 2
        assert result.songs[0].song_id == "song-1"
        assert result.songs[0].title == "Song One"
        assert result.songs[0].artist == "Artist One"
        assert result.songs[1].song_id == "song-2"
        assert result.songs[1].title == "Song Two"

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_detail_empty(self, mock_get):
        """get_playlist_detail() should raise PlaylistNotFoundError for empty response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(PlaylistNotFoundError):
            library.get_playlist_detail("nonexistent")

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_detail_subsonic_error(self, mock_get):
        """status='failed' (unknown id) should raise PlaylistNotFoundError."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 70, "message": "Playlist not found"},
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(PlaylistNotFoundError):
            library.get_playlist_detail("nonexistent")

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_get_playlist_detail_connection_error(self, mock_get):
        """get_playlist_detail() should raise NavidromeConnectionError on connection error."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(NavidromeConnectionError):
            library.get_playlist_detail("pl-1")


class TestNavidromeRemoveSongsFromPlaylist:
    """Test remove_songs_from_playlist() method."""

    @staticmethod
    def _entry(song_id: str) -> dict:
        return {
            "id": song_id,
            "title": f"Song {song_id}",
            "artist": "Artist",
            "album": "Album",
            "path": f"{song_id}.mp3",
            "duration": 180,
            "size": 4320000,
        }

    def _detail_response(self, song_ids: list[str]) -> Mock:
        """Build a getPlaylist response with the given song IDs in order."""
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "subsonic-response": {
                "status": "ok",
                "playlist": {
                    "id": "pl-1",
                    "name": "My Playlist",
                    "entry": [self._entry(sid) for sid in song_ids],
                },
            }
        }
        return response

    def _ok_response(self) -> Mock:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"subsonic-response": {"status": "ok"}}
        return response

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_song_resolves_correct_index(self, mock_get):
        """Removing one song should resolve its index and leave others untouched."""
        mock_get.side_effect = [
            self._detail_response(["song-1", "song-2", "song-3"]),
            self._ok_response(),
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-2"])

        assert result is True
        assert mock_get.call_count == 2
        remove_call = mock_get.call_args_list[1]
        assert remove_call.args[0] == "http://navidrome:4533/rest/updatePlaylist"
        assert remove_call.kwargs["params"]["songIndexToRemove"] == [1]

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_multiple_songs(self, mock_get):
        """Removing several songs should combine all their indices."""
        mock_get.side_effect = [
            self._detail_response(["song-1", "song-2", "song-3", "song-4"]),
            self._ok_response(),
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-1", "song-3"])

        assert result is True
        remove_call = mock_get.call_args_list[1]
        assert remove_call.kwargs["params"]["songIndexToRemove"] == [2, 0]

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_duplicates_keeps_last_occurrence(self, mock_get):
        """All copies except the most recently added one should be removed."""
        mock_get.side_effect = [
            self._detail_response(["song-1", "song-2", "song-1", "song-1", "song-3"]),
            self._ok_response(),
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-1"])

        assert result is True
        remove_call = mock_get.call_args_list[1]
        assert remove_call.kwargs["params"]["songIndexToRemove"] == [2, 0]

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_song_not_present_is_noop(self, mock_get):
        """Removing a song not in the playlist should be a no-op, not an error."""
        mock_get.side_effect = [
            self._detail_response(["song-1", "song-2"]),
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-99"])

        assert result is True
        assert mock_get.call_count == 1

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_from_empty_playlist_is_noop(self, mock_get):
        """Removing from an empty playlist should not raise."""
        mock_get.side_effect = [
            self._detail_response([]),
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-1"])

        assert result is True
        assert mock_get.call_count == 1

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_from_missing_playlist_returns_false(self, mock_get):
        """Removing from a missing playlist should return False, not raise."""
        missing = Mock()
        missing.status_code = 200
        missing.json.return_value = {
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 70, "message": "Playlist not found"},
            }
        }
        mock_get.side_effect = [missing]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("nonexistent", ["song-1"])

        assert result is False
        assert mock_get.call_count == 1

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_empty_song_list(self, mock_get):
        """Empty song list should return True without any API calls."""
        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", [])

        assert result is True
        mock_get.assert_not_called()

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_subsonic_failure(self, mock_get):
        """A failed updatePlaylist response should return False."""
        failed = Mock()
        failed.status_code = 200
        failed.json.return_value = {
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 1, "message": "Something went wrong"},
            }
        }
        mock_get.side_effect = [
            self._detail_response(["song-1", "song-2"]),
            failed,
        ]

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.remove_songs_from_playlist("pl-1", ["song-1"])

        assert result is False

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_remove_connection_error(self, mock_get):
        """A connection error should raise NavidromeConnectionError."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(NavidromeConnectionError):
            library.remove_songs_from_playlist("pl-1", ["song-1"])


class TestNavidromeDeletePlaylist:
    """Test delete_playlist() method."""

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_delete_playlist_success(self, mock_get):
        """delete_playlist() should return True on success."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"subsonic-response": {"status": "ok"}}
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.delete_playlist("pl-1")

        assert result is True
        call = mock_get.call_args
        assert call.args[0] == "http://navidrome:4533/rest/deletePlaylist"
        assert call.kwargs["params"]["id"] == "pl-1"
        assert "playlistId" not in call.kwargs["params"]

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_delete_playlist_subsonic_failure(self, mock_get):
        """A failed deletePlaylist response should return False."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 10, "message": "missing parameter: 'id'"},
            }
        }
        mock_get.return_value = mock_response

        config = MockConfig()
        library = NavidromeLibrary(config)

        result = library.delete_playlist("pl-1")

        assert result is False

    @patch('app.services.navidrome_library.requests.Session.get')
    def test_delete_playlist_connection_error(self, mock_get):
        """A connection error should raise NavidromeConnectionError."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        config = MockConfig()
        library = NavidromeLibrary(config)

        with pytest.raises(NavidromeConnectionError):
            library.delete_playlist("pl-1")


class TestGetSongRealPath:
    """P6.7-6: native-API real-path resolution (Subsonic `path` is
    tag-synthesized and unusable for file operations)."""

    def _library(self):
        return NavidromeLibrary(MockConfig())

    @patch("app.services.navidrome_library.requests.Session.post")
    @patch("app.services.navidrome_library.requests.Session.get")
    def test_resolves_real_path(self, mock_get, mock_post):
        login = Mock()
        login.status_code = 200
        login.json.return_value = {"token": "jwt-token"}
        mock_post.return_value = login

        song = Mock()
        song.status_code = 200
        song.json.return_value = {
            "id": "song-1",
            "path": "discovery/Comfort_Zone/artist/01 Track.flac",
        }
        mock_get.return_value = song

        path = self._library().get_song_real_path("song-1")

        assert path == "discovery/Comfort_Zone/artist/01 Track.flac"
        # The JWT goes in the header the native API expects.
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["X-ND-Authorization"] == "Bearer jwt-token"
        assert mock_get.call_args.args[0].endswith("/api/song/song-1")

    @patch("app.services.navidrome_library.requests.Session.post")
    @patch("app.services.navidrome_library.requests.Session.get")
    def test_caches_token_across_calls(self, mock_get, mock_post):
        login = Mock()
        login.status_code = 200
        login.json.return_value = {"token": "jwt-token"}
        mock_post.return_value = login

        song = Mock()
        song.status_code = 200
        song.json.return_value = {"path": "a.flac"}
        mock_get.return_value = song

        library = self._library()
        library.get_song_real_path("song-1")
        library.get_song_real_path("song-2")

        assert mock_post.call_count == 1

    @patch("app.services.navidrome_library.requests.Session.post")
    @patch("app.services.navidrome_library.requests.Session.get")
    def test_login_failure_returns_none(self, mock_get, mock_post):
        login = Mock()
        login.status_code = 401
        login.text = "nope"
        mock_post.return_value = login

        assert self._library().get_song_real_path("song-1") is None
        mock_get.assert_not_called()

    @patch("app.services.navidrome_library.requests.Session.post")
    @patch("app.services.navidrome_library.requests.Session.get")
    def test_missing_path_returns_none(self, mock_get, mock_post):
        login = Mock()
        login.status_code = 200
        login.json.return_value = {"token": "jwt-token"}
        mock_post.return_value = login

        song = Mock()
        song.status_code = 200
        song.json.return_value = {"id": "song-1"}
        mock_get.return_value = song

        assert self._library().get_song_real_path("song-1") is None

    @patch("app.services.navidrome_library.requests.Session.post")
    @patch("app.services.navidrome_library.requests.Session.get")
    def test_retries_once_on_expired_token(self, mock_get, mock_post):
        login = Mock()
        login.status_code = 200
        login.json.return_value = {"token": "jwt-token"}
        mock_post.return_value = login

        unauthorized = Mock()
        unauthorized.status_code = 401
        ok = Mock()
        ok.status_code = 200
        ok.json.return_value = {"path": "a.flac"}
        mock_get.side_effect = [unauthorized, ok]

        path = self._library().get_song_real_path("song-1")

        assert path == "a.flac"
        assert mock_get.call_count == 2
