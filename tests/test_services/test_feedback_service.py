"""
Unit tests for FeedbackService interface.
"""

import pytest

from app.services.interfaces.feedback import FeedbackService, SyncResult
from app.services.library import Song


class MockFeedbackService(FeedbackService):
    """Mock implementation for testing."""
    
    def __init__(self):
        self.feedback_log = []  # Track all feedback submissions
        self.fail_mbid_pattern = "fail"  # MBIDs containing this will fail
    
    def send_feedback(self, mbid: str, score: int) -> bool:
        """Submit feedback with mock failure logic."""
        if score not in (1, -1):
            raise ValueError(f"Score must be +1 or -1, got {score}")
        
        # Simulate failure for specific MBID pattern
        if self.fail_mbid_pattern in mbid.lower():
            return False
        
        self.feedback_log.append({"mbid": mbid, "score": score})
        return True
    
    def sync_loves(self, starred: list) -> SyncResult:
        """Sync starred songs as loves."""
        synced = 0
        failed = 0
        failures = []
        
        for song in starred:
            if not song.mbid:
                continue
            
            success = self.send_feedback(song.mbid, 1)
            if success:
                synced += 1
            else:
                failed += 1
                failures.append({
                    "song_id": song.song_id,
                    "mbid": song.mbid,
                    "message": "Feedback submission failed"
                })
        
        return SyncResult(
            synced_count=synced,
            failed_count=failed,
            failures=failures
        )
    
    def sync_hates(self, trashed: list) -> SyncResult:
        """Sync trashed songs as hates."""
        synced = 0
        failed = 0
        failures = []
        
        for song in trashed:
            if not song.mbid:
                continue
            
            success = self.send_feedback(song.mbid, -1)
            if success:
                synced += 1
            else:
                failed += 1
                failures.append({
                    "song_id": song.song_id,
                    "mbid": song.mbid,
                    "message": "Feedback submission failed"
                })
        
        return SyncResult(
            synced_count=synced,
            failed_count=failed,
            failures=failures
        )


class TestFeedbackServiceInterface:
    """Test FeedbackService interface contract."""
    
    def test_cannot_instantiate_abstract_class(self):
        """FeedbackService is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            FeedbackService()
    
    def test_concrete_implementation_must_implement_all_methods(self):
        """Concrete implementation must implement all abstract methods."""
        service = MockFeedbackService()
        assert isinstance(service, FeedbackService)
    
    def test_incomplete_implementation_raises_error(self):
        """Incomplete implementation should raise TypeError."""
        class IncompleteFeedbackService(FeedbackService):
            def send_feedback(self, mbid, score):
                pass
            # Missing other methods
        
        with pytest.raises(TypeError):
            IncompleteFeedbackService()


class TestFeedbackServiceMock:
    """Test mock implementation of FeedbackService."""
    
    def test_send_feedback_love(self):
        """send_feedback() should accept +1 for love."""
        service = MockFeedbackService()
        result = service.send_feedback("mbid-123", 1)
        
        assert result is True
        assert len(service.feedback_log) == 1
        assert service.feedback_log[0] == {"mbid": "mbid-123", "score": 1}
    
    def test_send_feedback_hate(self):
        """send_feedback() should accept -1 for hate."""
        service = MockFeedbackService()
        result = service.send_feedback("mbid-456", -1)
        
        assert result is True
        assert len(service.feedback_log) == 1
        assert service.feedback_log[0] == {"mbid": "mbid-456", "score": -1}
    
    def test_send_feedback_invalid_score_zero(self):
        """send_feedback() should reject score 0."""
        service = MockFeedbackService()
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            service.send_feedback("mbid-123", 0)
    
    def test_send_feedback_invalid_score_positive(self):
        """send_feedback() should reject score > 1."""
        service = MockFeedbackService()
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            service.send_feedback("mbid-123", 2)
    
    def test_send_feedback_invalid_score_negative(self):
        """send_feedback() should reject score < -1."""
        service = MockFeedbackService()
        
        with pytest.raises(ValueError, match="Score must be \\+1 or -1"):
            service.send_feedback("mbid-123", -2)
    
    def test_send_feedback_failure(self):
        """send_feedback() should return False on failure."""
        service = MockFeedbackService()
        result = service.send_feedback("fail-mbid", 1)
        
        assert result is False
        assert len(service.feedback_log) == 0  # Not logged on failure
    
    def test_sync_loves_all_success(self):
        """sync_loves() should sync all starred songs."""
        service = MockFeedbackService()
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "mbid-2"),
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = service.sync_loves(starred)
        
        assert result.synced_count == 3
        assert result.failed_count == 0
        assert len(result.failures) == 0
        assert len(service.feedback_log) == 3
        assert all(log["score"] == 1 for log in service.feedback_log)
    
    def test_sync_loves_with_failures(self):
        """sync_loves() should handle partial failures."""
        service = MockFeedbackService()
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "fail-mbid"),  # Will fail
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = service.sync_loves(starred)
        
        assert result.synced_count == 2
        assert result.failed_count == 1
        assert len(result.failures) == 1
        assert result.failures[0]["song_id"] == "song-2"
    
    def test_sync_loves_skip_no_mbid(self):
        """sync_loves() should skip songs without MBID."""
        service = MockFeedbackService()
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, None),  # No MBID
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        
        result = service.sync_loves(starred)
        
        assert result.synced_count == 2  # Only 2 synced (song-2 skipped)
        assert result.failed_count == 0
        assert len(service.feedback_log) == 2
    
    def test_sync_loves_empty_list(self):
        """sync_loves() should handle empty list."""
        service = MockFeedbackService()
        result = service.sync_loves([])
        
        assert result.synced_count == 0
        assert result.failed_count == 0
        assert len(result.failures) == 0
    
    def test_sync_hates_all_success(self):
        """sync_hates() should sync all trashed songs."""
        service = MockFeedbackService()
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = service.sync_hates(trashed)
        
        assert result.synced_count == 2
        assert result.failed_count == 0
        assert len(result.failures) == 0
        assert len(service.feedback_log) == 2
        assert all(log["score"] == -1 for log in service.feedback_log)
    
    def test_sync_hates_with_failures(self):
        """sync_hates() should handle partial failures."""
        service = MockFeedbackService()
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, "fail-mbid"),  # Will fail
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = service.sync_hates(trashed)
        
        assert result.synced_count == 1
        assert result.failed_count == 1
        assert len(result.failures) == 1
        assert result.failures[0]["song_id"] == "song-1"
    
    def test_sync_hates_skip_no_mbid(self):
        """sync_hates() should skip songs without MBID."""
        service = MockFeedbackService()
        trashed = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 0, False, None),  # No MBID
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 0, False, "mbid-2")
        ]
        
        result = service.sync_hates(trashed)
        
        assert result.synced_count == 1  # Only 1 synced (song-1 skipped)
        assert result.failed_count == 0
    
    def test_sync_hates_empty_list(self):
        """sync_hates() should handle empty list."""
        service = MockFeedbackService()
        result = service.sync_hates([])
        
        assert result.synced_count == 0
        assert result.failed_count == 0
        assert len(result.failures) == 0


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
        assert result.failures[0]["song_id"] == "song-1"
    
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
    
    def test_sync_result_all_failures(self):
        """SyncResult should represent all failures."""
        result = SyncResult(
            synced_count=0,
            failed_count=5,
            failures=[
                {"song_id": f"song-{i}", "mbid": f"mbid-{i}", "message": "Failed"}
                for i in range(5)
            ]
        )
        
        assert result.synced_count == 0
        assert result.failed_count == 5
        assert len(result.failures) == 5


class TestFeedbackWorkflow:
    """Integration-style tests for feedback workflows."""
    
    def test_love_then_hate_workflow(self):
        """Test workflow: love songs, then hate different songs."""
        service = MockFeedbackService()
        
        # Love some songs
        starred = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1"),
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "mbid-2")
        ]
        love_result = service.sync_loves(starred)
        
        assert love_result.synced_count == 2
        assert len(service.feedback_log) == 2
        assert all(log["score"] == 1 for log in service.feedback_log)
        
        # Hate different songs
        trashed = [
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 0, False, "mbid-3"),
            Song("song-4", "Track 4", "Artist 4", "Album", "/path4", 240, 5760000, 192, 4, 2020, "Rock", 0, False, "mbid-4")
        ]
        hate_result = service.sync_hates(trashed)
        
        assert hate_result.synced_count == 2
        assert len(service.feedback_log) == 4  # 2 loves + 2 hates
        assert sum(1 for log in service.feedback_log if log["score"] == 1) == 2
        assert sum(1 for log in service.feedback_log if log["score"] == -1) == 2
    
    def test_incremental_sync_workflow(self):
        """Test workflow: sync in batches (simulating incremental sync)."""
        service = MockFeedbackService()
        
        # First batch
        batch1 = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1")
        ]
        result1 = service.sync_loves(batch1)
        assert result1.synced_count == 1
        
        # Second batch (new songs)
        batch2 = [
            Song("song-2", "Track 2", "Artist 2", "Album", "/path2", 200, 4800000, 192, 2, 2020, "Rock", 5, True, "mbid-2"),
            Song("song-3", "Track 3", "Artist 3", "Album", "/path3", 220, 5280000, 192, 3, 2020, "Rock", 5, True, "mbid-3")
        ]
        result2 = service.sync_loves(batch2)
        assert result2.synced_count == 2
        
        # Total
        assert len(service.feedback_log) == 3
