"""
Unit tests for exception hierarchy.
"""

import pytest

from app.exceptions import (
    # Base
    MusicaError,
    # Search
    SearchError, SearchNotFoundError, SearchInitiationError, SearchTimeoutError,
    # Download
    DownloadError, TransferNotFoundError, NoViablePeerError, QueueError, MaxRetriesExceededError,
    # Service Connection
    ServiceConnectionError, NavidromeConnectionError, SlskdConnectionError, ListenBrainzConnectionError,
    # ListenBrainz
    ListenBrainzError, ListenBrainzDisabledError, ListenBrainzFeedbackError,
    # Library
    LibraryError, PlaylistNotFoundError, PlaylistError,
    # Recommendation
    RecommendationError, RecommendationFetchError, ClassificationError,
    # Config
    ConfigError, ConfigValidationError, ConfigNotFoundError
)


class TestMusicaErrorBase:
    """Test base MusicaError functionality."""
    
    def test_basic_creation(self):
        """MusicaError should be creatable with message and code."""
        error = MusicaError("Test error", "TEST_ERROR")
        
        assert error.message == "Test error"
        assert error.code == "TEST_ERROR"
        assert error.details == {}
    
    def test_with_details(self):
        """MusicaError should accept details dict."""
        error = MusicaError("Test error", "TEST_ERROR", {"key": "value"})
        
        assert error.details == {"key": "value"}
    
    def test_default_code(self):
        """MusicaError should default to UNKNOWN_ERROR code."""
        error = MusicaError("Test error")
        
        assert error.code == "UNKNOWN_ERROR"
    
    def test_str_representation(self):
        """MusicaError __str__ should include code and message."""
        error = MusicaError("Test error", "TEST_ERROR")
        
        assert str(error) == "[TEST_ERROR] Test error"
    
    def test_to_dict(self):
        """MusicaError.to_dict() should produce correct format."""
        error = MusicaError("Test error", "TEST_ERROR", {"key": "value"})
        result = error.to_dict()
        
        assert result == {
            "error": {
                "code": "TEST_ERROR",
                "message": "Test error",
                "details": {"key": "value"}
            }
        }
    
    def test_to_dict_empty_details(self):
        """MusicaError.to_dict() should handle empty details."""
        error = MusicaError("Test error", "TEST_ERROR")
        result = error.to_dict()
        
        assert result["error"]["details"] == {}
    
    def test_inheritance_from_exception(self):
        """MusicaError should inherit from Exception."""
        error = MusicaError("Test error")
        
        assert isinstance(error, Exception)
    
    def test_can_be_raised_and_caught(self):
        """MusicaError should be raisable and catchable."""
        with pytest.raises(MusicaError) as exc_info:
            raise MusicaError("Test error", "TEST_ERROR")
        
        assert exc_info.value.message == "Test error"
        assert exc_info.value.code == "TEST_ERROR"


class TestSearchExceptions:
    """Test search-related exceptions."""
    
    def test_search_error_inheritance(self):
        """SearchError should inherit from MusicaError."""
        error = SearchError("Search failed")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, SearchError)
    
    def test_search_not_found_error(self):
        """SearchNotFoundError should have correct attributes."""
        error = SearchNotFoundError("search-123")
        
        assert error.message == "Search 'search-123' not found"
        assert error.code == "SEARCH_NOT_FOUND"
        assert error.details == {"search_id": "search-123"}
    
    def test_search_initiation_error(self):
        """SearchInitiationError should have correct attributes."""
        error = SearchInitiationError("Bohemian Rhapsody", "slskd offline")
        
        assert "Bohemian Rhapsody" in error.message
        assert "slskd offline" in error.message
        assert error.code == "SEARCH_INITIATION_FAILED"
        assert error.details["query"] == "Bohemian Rhapsody"
        assert error.details["reason"] == "slskd offline"
    
    def test_search_initiation_error_default_reason(self):
        """SearchInitiationError should have default reason."""
        error = SearchInitiationError("Test")
        
        assert "Unknown error" in error.message
    
    def test_search_timeout_error(self):
        """SearchTimeoutError should have correct attributes."""
        error = SearchTimeoutError("search-123", 10)
        
        assert error.message == "Search 'search-123' timed out after 10s"
        assert error.code == "SEARCH_TIMEOUT"
        assert error.details["search_id"] == "search-123"
        assert error.details["timeout_seconds"] == 10


class TestDownloadExceptions:
    """Test download-related exceptions."""
    
    def test_download_error_inheritance(self):
        """DownloadError should inherit from MusicaError."""
        error = DownloadError("Download failed")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, DownloadError)
    
    def test_transfer_not_found_error(self):
        """TransferNotFoundError should have correct attributes."""
        error = TransferNotFoundError("transfer-123")
        
        assert error.message == "Transfer 'transfer-123' not found"
        assert error.code == "TRANSFER_NOT_FOUND"
        assert error.details == {"transfer_id": "transfer-123"}
    
    def test_no_viable_peer_error(self):
        """NoViablePeerError should have correct attributes."""
        error = NoViablePeerError("song.mp3", "All peers busy")
        
        assert "song.mp3" in error.message
        assert "All peers busy" in error.message
        assert error.code == "NO_VIABLE_PEER"
        assert error.details["filename"] == "song.mp3"
        assert error.details["reason"] == "All peers busy"
    
    def test_no_viable_peer_error_default_reason(self):
        """NoViablePeerError should have default reason."""
        error = NoViablePeerError("song.mp3")
        
        assert "No peers with free upload slots" in error.message
    
    def test_queue_error(self):
        """QueueError should have correct attributes."""
        error = QueueError("peer1", ["song1.mp3", "song2.mp3"], "Connection lost")
        
        assert "peer1" in error.message
        assert "Connection lost" in error.message
        assert error.code == "QUEUE_FAILED"
        assert error.details["username"] == "peer1"
        assert error.details["files"] == ["song1.mp3", "song2.mp3"]
    
    def test_max_retries_exceeded_error(self):
        """MaxRetriesExceededError should have correct attributes."""
        error = MaxRetriesExceededError("transfer-123", 3)
        
        assert error.message == "Max retries (3) exceeded for transfer 'transfer-123'"
        assert error.code == "MAX_RETRIES_EXCEEDED"
        assert error.details["transfer_id"] == "transfer-123"
        assert error.details["max_retries"] == 3


class TestServiceConnectionExceptions:
    """Test service connection exceptions."""
    
    def test_service_connection_error_inheritance(self):
        """ServiceConnectionError should inherit from MusicaError."""
        error = ServiceConnectionError("Connection failed")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, ServiceConnectionError)
    
    def test_navidrome_connection_error(self):
        """NavidromeConnectionError should have correct attributes."""
        error = NavidromeConnectionError("http://navidrome:4533", "Timeout")
        
        assert "http://navidrome:4533" in error.message
        assert "Timeout" in error.message
        assert error.code == "NAVIDROME_CONNECTION_FAILED"
        assert error.details["url"] == "http://navidrome:4533"
    
    def test_navidrome_connection_error_default_reason(self):
        """NavidromeConnectionError should have default reason."""
        error = NavidromeConnectionError("http://navidrome:4533")
        
        assert "Connection failed" in error.message
    
    def test_slskd_connection_error(self):
        """SlskdConnectionError should have correct attributes."""
        error = SlskdConnectionError("http://slskd:5030", "Refused")
        
        assert error.code == "SLSKD_CONNECTION_FAILED"
        assert error.details["url"] == "http://slskd:5030"
    
    def test_listenbrainz_connection_error(self):
        """ListenBrainzConnectionError should have correct attributes."""
        error = ListenBrainzConnectionError("https://api.listenbrainz.org", "DNS error")
        
        assert error.code == "LISTENBRAINZ_CONNECTION_FAILED"
        assert error.details["url"] == "https://api.listenbrainz.org"


class TestListenBrainzExceptions:
    """Test ListenBrainz-specific exceptions."""
    
    def test_listenbrainz_error_inheritance(self):
        """ListenBrainzError should inherit from MusicaError."""
        error = ListenBrainzError("LB error")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, ListenBrainzError)
    
    def test_listenbrainz_disabled_error(self):
        """ListenBrainzDisabledError should have correct attributes."""
        error = ListenBrainzDisabledError()
        
        assert "disabled" in error.message.lower()
        assert "enabled = true" in error.message
        assert error.code == "LISTENBRAINZ_DISABLED"
        assert error.details == {}
    
    def test_listenbrainz_feedback_error(self):
        """ListenBrainzFeedbackError should have correct attributes."""
        error = ListenBrainzFeedbackError("mbid-123", 1, "Rate limited")
        
        assert "mbid-123" in error.message
        assert "score=1" in error.message
        assert "Rate limited" in error.message
        assert error.code == "LISTENBRAINZ_FEEDBACK_FAILED"
        assert error.details["mbid"] == "mbid-123"
        assert error.details["score"] == 1


class TestLibraryExceptions:
    """Test library-related exceptions."""
    
    def test_library_error_inheritance(self):
        """LibraryError should inherit from MusicaError."""
        error = LibraryError("Library error")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, LibraryError)
    
    def test_playlist_not_found_error(self):
        """PlaylistNotFoundError should have correct attributes."""
        error = PlaylistNotFoundError("playlist-123")
        
        assert error.message == "Playlist 'playlist-123' not found"
        assert error.code == "PLAYLIST_NOT_FOUND"
        assert error.details == {"playlist_id": "playlist-123"}
    
    def test_playlist_error(self):
        """PlaylistError should have correct attributes."""
        error = PlaylistError("playlist-123", "update", "Permission denied")
        
        assert "playlist-123" in error.message
        assert "update" in error.message
        assert "Permission denied" in error.message
        assert error.code == "PLAYLIST_OPERATION_FAILED"
        assert error.details["operation"] == "update"


class TestRecommendationExceptions:
    """Test recommendation-related exceptions."""
    
    def test_recommendation_error_inheritance(self):
        """RecommendationError should inherit from MusicaError."""
        error = RecommendationError("Rec error")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, RecommendationError)
    
    def test_recommendation_fetch_error(self):
        """RecommendationFetchError should have correct attributes."""
        error = RecommendationFetchError("comfort_zone", "API timeout")
        
        assert "comfort_zone" in error.message
        assert "API timeout" in error.message
        assert error.code == "RECOMMENDATION_FETCH_FAILED"
        assert error.details["source"] == "comfort_zone"
    
    def test_classification_error(self):
        """ClassificationError should have correct attributes."""
        error = ClassificationError("Invalid library data")
        
        assert "classify" in error.message.lower()
        assert "Invalid library data" in error.message
        assert error.code == "CLASSIFICATION_FAILED"


class TestConfigExceptions:
    """Test configuration-related exceptions."""
    
    def test_config_error_inheritance(self):
        """ConfigError should inherit from MusicaError."""
        error = ConfigError("Config error")
        
        assert isinstance(error, MusicaError)
        assert isinstance(error, ConfigError)
    
    def test_config_validation_error(self):
        """ConfigValidationError should have correct attributes."""
        error = ConfigValidationError("search.wait_seconds", -5, "Must be positive")
        
        assert "search.wait_seconds" in error.message
        assert "Must be positive" in error.message
        assert "-5" in error.message
        assert error.code == "CONFIG_VALIDATION_FAILED"
        assert error.details["key"] == "search.wait_seconds"
        assert error.details["value"] == "-5"
    
    def test_config_not_found_error(self):
        """ConfigNotFoundError should have correct attributes."""
        error = ConfigNotFoundError("navidrome.url")
        
        assert error.message == "Required configuration 'navidrome.url' not found"
        assert error.code == "CONFIG_NOT_FOUND"
        assert error.details == {"key": "navidrome.url"}


class TestExceptionHierarchy:
    """Test exception hierarchy relationships."""
    
    def test_all_exceptions_inherit_from_musica_error(self):
        """All custom exceptions should inherit from MusicaError."""
        exceptions = [
            SearchError("test"),
            SearchNotFoundError("test"),
            DownloadError("test"),
            TransferNotFoundError("test"),
            ServiceConnectionError("test"),
            NavidromeConnectionError("test"),
            ListenBrainzError("test"),
            ListenBrainzDisabledError(),
            LibraryError("test"),
            PlaylistNotFoundError("test"),
            RecommendationError("test"),
            ConfigError("test")
        ]
        
        for error in exceptions:
            assert isinstance(error, MusicaError)
            assert isinstance(error, Exception)
    
    def test_error_codes_are_unique(self):
        """All error codes should be unique."""
        errors = [
            SearchNotFoundError("test"),
            SearchInitiationError("test"),
            SearchTimeoutError("test", 10),
            TransferNotFoundError("test"),
            NoViablePeerError("test"),
            QueueError("test", []),
            MaxRetriesExceededError("test", 3),
            NavidromeConnectionError("test"),
            SlskdConnectionError("test"),
            ListenBrainzConnectionError("test"),
            ListenBrainzDisabledError(),
            ListenBrainzFeedbackError("test", 1),
            PlaylistNotFoundError("test"),
            PlaylistError("test", "test"),
            RecommendationFetchError("test"),
            ClassificationError(),
            ConfigValidationError("test", "test"),
            ConfigNotFoundError("test")
        ]
        
        codes = [error.code for error in errors]
        assert len(codes) == len(set(codes)), f"Duplicate codes found: {[c for c in codes if codes.count(c) > 1]}"
    
    def test_can_catch_by_base_class(self):
        """Should be able to catch specific exceptions by base class."""
        # Catch SearchNotFoundError by SearchError
        with pytest.raises(SearchError):
            raise SearchNotFoundError("test")
        
        # Catch NavidromeConnectionError by ServiceConnectionError
        with pytest.raises(ServiceConnectionError):
            raise NavidromeConnectionError("test")
        
        # Catch ListenBrainzDisabledError by ListenBrainzError
        with pytest.raises(ListenBrainzError):
            raise ListenBrainzDisabledError()
        
        # Catch all by MusicaError
        with pytest.raises(MusicaError):
            raise SearchNotFoundError("test")
