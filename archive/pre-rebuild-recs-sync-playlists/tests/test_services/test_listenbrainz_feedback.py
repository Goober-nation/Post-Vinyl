"""
Unit tests for ListenBrainzFeedback implementation.
"""

import pytest
from unittest.mock import Mock, patch

from app.services.feedback import ListenBrainzFeedback
from app.services.interfaces.feedback import SyncResult
from app.services.library import Song
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError
)
from app.config import Config


class MockConfig:
    """Mock config for testing."""
    
    class ListenBrainzConfig:
        enabled = True
        url = "https://api.listenbrainz.org"
        token = "test-token"
        username = "testuser"
    
    def __init__(self):
        self.listenbrainz = self.ListenBrainzConfig()


class TestListenBrainzFeedbackInit:
    """Test ListenBrainzFeedback initialization."""
    
    def test_init_with_config(self):
        """ListenBrainzFeedback should initialize with config."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        assert feedback.base_url == "https://api.listenbrainz.org"
        assert feedback.token == "test-token"
        assert feedback.username == "testuser"
    
    def test_get_headers_with_token(self):
        """_get_headers should include authorization token."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        headers = feedback._get_headers()
        
        assert headers["Authorization"] == "Token test-token"
        assert headers["Content-Type"] == "application/json"


class TestListenBrainzSendFeedback:
    """Test send_feedback() method."""
    
    @patch('app.services.feedback.requests.Session.post')
    def test_send_feedback_love_success(self, mock_post):
        """send_feedback() should send love (+1) successfully."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.send_feedback("mbid-123", 1)
        
        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["recording_mbid"] == "mbid-123"
        assert call_args[1]["json"]["score"] == 1
    
    @patch('app.services.feedback.requests.Session.post')
    def test_send_feedback_hate_success(self, mock_post):
        """send_feedback() should send hate (-1) successfully."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.send_feedback("mbid-456", -1)
        
        assert result is True
        call_args = mock_post.call_args
        assert call_args[1]["json"]["score"] == -1
    
    def test_send_feedback_invalid_score_zero(self):
        """send_feedback() should raise ValueError for score 0."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            feedback.send_feedback("mbid-123", 0)
    
    def test_send_feedback_invalid_score_positive(self):
        """send_feedback() should raise ValueError for score > 1."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            feedback.send_feedback("mbid-123", 2)
    
    def test_send_feedback_invalid_score_negative(self):
        """send_feedback() should raise ValueError for score < -1."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            feedback.send_feedback("mbid-123", -2)
    
    @patch('app.services.feedback.requests.Session.post')
    def test_send_feedback_no_mbid(self, mock_post):
        """send_feedback() should return False for empty MBID."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.send_feedback("", 1)
        
        assert result is False
        mock_post.assert_not_called()
    
    @patch('app.services.feedback.requests.Session.post')
    def test_send_feedback_http_error(self, mock_post):
        """send_feedback() should return False on HTTP error."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.send_feedback("mbid-123", 1)
        
        assert result is False
    
    @patch('app.services.feedback.requests.Session.post')
    def test_send_feedback_connection_error(self, mock_post):
        """send_feedback() should raise ListenBrainzConnectionError on connection error."""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ListenBrainzConnectionError):
            feedback.send_feedback("mbid-123", 1)
    
    def test_send_feedback_disabled(self):
        """send_feedback() should raise ListenBrainzDisabledError when disabled."""
        config = MockConfig()
        config.listenbrainz.enabled = False
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ListenBrainzDisabledError):
            feedback.send_feedback("mbid-123", 1)


class TestListenBrainzSyncLoves:
    """Test sync_loves() method."""
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_loves_all_success(self, mock_send):
        """sync_loves() should sync all songs with MBIDs."""
        mock_send.return_value = True
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "mbid-2"),
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = feedback.sync_loves(starred)
        
        assert isinstance(result, SyncResult)
        assert result.synced_count == 3
        assert result.failed_count == 0
        assert len(result.failures) == 0
        assert mock_send.call_count == 3
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_loves_with_failures(self, mock_send):
        """sync_loves() should handle partial failures."""
        mock_send.side_effect = [True, False, True]
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "mbid-2"),
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = feedback.sync_loves(starred)
        
        assert result.synced_count == 2
        assert result.failed_count == 1
        assert len(result.failures) == 1
        assert result.failures[0]["song_id"] == "song-2"
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_loves_skip_no_mbid(self, mock_send):
        """sync_loves() should skip songs without MBID."""
        mock_send.return_value = True
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, None),
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = feedback.sync_loves(starred)
        
        assert result.synced_count == 2
        assert result.failed_count == 0
        assert mock_send.call_count == 2
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_loves_empty_list(self, mock_send):
        """sync_loves() should handle empty list."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.sync_loves([])
        
        assert result.synced_count == 0
        assert result.failed_count == 0
        assert len(result.failures) == 0
        mock_send.assert_not_called()
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_loves_with_exception(self, mock_send):
        """sync_loves() should handle exceptions gracefully."""
        mock_send.side_effect = Exception("Unexpected error")
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1")
        ]
        
        result = feedback.sync_loves(starred)
        
        assert result.synced_count == 0
        assert result.failed_count == 1
        assert len(result.failures) == 1
        assert "Unexpected error" in result.failures[0]["message"]
    
    def test_sync_loves_disabled(self):
        """sync_loves() should raise ListenBrainzDisabledError when disabled."""
        config = MockConfig()
        config.listenbrainz.enabled = False
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ListenBrainzDisabledError):
            feedback.sync_loves([])


class TestListenBrainzSyncHates:
    """Test sync_hates() method."""
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_hates_all_success(self, mock_send):
        """sync_hates() should sync all songs with MBIDs."""
        mock_send.return_value = True
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = feedback.sync_hates(trashed)
        
        assert isinstance(result, SyncResult)
        assert result.synced_count == 2
        assert result.failed_count == 0
        assert len(result.failures) == 0
        assert mock_send.call_count == 2
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_hates_with_failures(self, mock_send):
        """sync_hates() should handle partial failures."""
        mock_send.side_effect = [False, True]
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = feedback.sync_hates(trashed)
        
        assert result.synced_count == 1
        assert result.failed_count == 1
        assert len(result.failures) == 1
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_hates_skip_no_mbid(self, mock_send):
        """sync_hates() should skip songs without MBID."""
        mock_send.return_value = True
        
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, None),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = feedback.sync_hates(trashed)
        
        assert result.synced_count == 1
        assert result.failed_count == 0
        assert mock_send.call_count == 1
    
    @patch('app.services.feedback.ListenBrainzFeedback.send_feedback')
    def test_sync_hates_empty_list(self, mock_send):
        """sync_hates() should handle empty list."""
        config = MockConfig()
        feedback = ListenBrainzFeedback(config)
        
        result = feedback.sync_hates([])
        
        assert result.synced_count == 0
        assert result.failed_count == 0
        assert len(result.failures) == 0
        mock_send.assert_not_called()
    
    def test_sync_hates_disabled(self):
        """sync_hates() should raise ListenBrainzDisabledError when disabled."""
        config = MockConfig()
        config.listenbrainz.enabled = False
        feedback = ListenBrainzFeedback(config)
        
        with pytest.raises(ListenBrainzDisabledError):
            feedback.sync_hates([])


class TestSyncResultDataclass:
    """Test SyncResult dataclass."""
    
    def test_sync_result_creation(self):
        """SyncResult should be creatable with all fields."""
        result = SyncResult(
            synced_count=5,
            failed_count=2,
            failures=[
                {"song_id": "song-1", "mbid": "mbid-1", "message": "Failed"},
                {"song_id": "song-2", "mbid": "mbid-2", "message": "Failed"}
            ]
        )
        
        assert result.synced_count == 5
        assert result.failed_count == 2
        assert len(result.failures) == 2
    
    def test_sync_result_no_failures(self):
        """SyncResult should allow empty failures list."""
        result = SyncResult(
            synced_count=10,
            failed_count=0,
            failures=[]
        )
        
        assert result.synced_count == 10
        assert result.failed_count == 0
        assert len(result.failures) == 0
