"""
Unit tests for DownloadService interface.
"""

import pytest
from datetime import datetime
from typing import Optional

from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    Transfer,
    RetryResult
)


class MockDownloadService(DownloadService):
    """Mock implementation for testing."""
    
    def __init__(self):
        self.transfers = {}
        self.next_id = 1
        self.stored_responses = {}  # For retry testing
    
    def queue(
        self,
        username: str,
        files: list[dict],
        search_id: Optional[str] = None,
        destination: Optional[str] = None
    ) -> QueueResult:
        failures = []
        enqueued = 0
        
        for file in files:
            filename = file.get("filename", "")
            size = file.get("size", 0)
            
            # Simulate failure for specific pattern
            if "fail" in filename.lower():
                failures.append({
                    "filename": filename,
                    "message": "Simulated failure"
                })
                continue
            
            transfer_id = f"transfer-{self.next_id}"
            self.next_id += 1
            
            transfer = Transfer(
                transfer_id=transfer_id,
                username=username,
                filename=filename,
                size=size,
                state="queued",
                progress=0.0,
                speed=None,
                started_at=datetime.now(),
                completed_at=None,
                is_rec_download="discovery" in destination.lower() if destination else False
            )
            self.transfers[transfer_id] = transfer
            enqueued += 1
            
            # Store search responses for retry testing
            if search_id:
                self.stored_responses[transfer_id] = {
                    "search_id": search_id,
                    "username": username,
                    "filename": filename
                }
        
        return QueueResult(
            enqueued_count=enqueued,
            failures=failures,
            search_id=search_id
        )
    
    def get_status(self) -> list[Transfer]:
        return list(self.transfers.values())
    
    def retry(self, transfer_id: str) -> RetryResult:
        if transfer_id not in self.transfers:
            raise ValueError(f"Transfer {transfer_id} not found")
        
        old_transfer = self.transfers[transfer_id]
        
        # Simulate no viable peer
        if "nopeer" in old_transfer.username.lower():
            return RetryResult(
                success=False,
                message="No viable peer found"
            )
        
        # Create new transfer
        new_transfer_id = f"transfer-{self.next_id}"
        self.next_id += 1
        
        new_transfer = Transfer(
            transfer_id=new_transfer_id,
            username=f"retry-peer-{self.next_id}",
            filename=old_transfer.filename,
            size=old_transfer.size,
            state="queued",
            progress=0.0,
            speed=None,
            started_at=datetime.now(),
            completed_at=None,
            is_rec_download=old_transfer.is_rec_download
        )
        self.transfers[new_transfer_id] = new_transfer
        
        return RetryResult(
            success=True,
            message="Retrying from new peer",
            new_transfer_id=new_transfer_id
        )
    
    def cancel(self, transfer_id: str) -> bool:
        if transfer_id not in self.transfers:
            raise ValueError(f"Transfer {transfer_id} not found")

        self.transfers[transfer_id].state = "cancelled"
        return True

    def delete_transfer(self, transfer_id: str) -> bool:
        if transfer_id not in self.transfers:
            return False
        del self.transfers[transfer_id]
        return True

    def get_transfer(self, transfer_id: str) -> Transfer:
        if transfer_id not in self.transfers:
            raise ValueError(f"Transfer {transfer_id} not found")
        return self.transfers[transfer_id]


class TestDownloadServiceInterface:
    """Test DownloadService interface contract."""
    
    def test_cannot_instantiate_abstract_class(self):
        """DownloadService is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            DownloadService()
    
    def test_concrete_implementation_must_implement_all_methods(self):
        """Concrete implementation must implement all abstract methods."""
        service = MockDownloadService()
        assert isinstance(service, DownloadService)
    
    def test_incomplete_implementation_raises_error(self):
        """Incomplete implementation should raise TypeError."""
        class IncompleteDownloadService(DownloadService):
            def queue(self, username, files, search_id=None, destination=None):
                pass
            # Missing other methods
        
        with pytest.raises(TypeError):
            IncompleteDownloadService()


class TestDownloadServiceMock:
    """Test mock implementation of DownloadService."""
    
    def test_queue_single_file(self):
        """queue() should enqueue a single file."""
        service = MockDownloadService()
        result = service.queue("peer1", [{"filename": "song.mp3", "size": 5242880}])
        
        assert result.enqueued_count == 1
        assert len(result.failures) == 0
        assert len(service.transfers) == 1
    
    def test_queue_multiple_files(self):
        """queue() should enqueue multiple files."""
        service = MockDownloadService()
        files = [
            {"filename": "song1.mp3", "size": 1000000},
            {"filename": "song2.mp3", "size": 2000000},
            {"filename": "song3.mp3", "size": 3000000}
        ]
        result = service.queue("peer1", files)
        
        assert result.enqueued_count == 3
        assert len(result.failures) == 0
        assert len(service.transfers) == 3
    
    def test_queue_with_failures(self):
        """queue() should handle partial failures."""
        service = MockDownloadService()
        files = [
            {"filename": "song.mp3", "size": 1000000},
            {"filename": "fail_song.mp3", "size": 2000000},  # Will fail
            {"filename": "another.mp3", "size": 3000000}
        ]
        result = service.queue("peer1", files)
        
        assert result.enqueued_count == 2
        assert len(result.failures) == 1
        assert result.failures[0]["filename"] == "fail_song.mp3"
    
    def test_queue_with_search_id(self):
        """queue() should accept and store search_id."""
        service = MockDownloadService()
        result = service.queue(
            "peer1",
            [{"filename": "song.mp3", "size": 1000000}],
            search_id="search-123"
        )
        
        assert result.search_id == "search-123"
        transfer_id = list(service.transfers.keys())[0]
        assert service.stored_responses[transfer_id]["search_id"] == "search-123"
    
    def test_queue_with_destination(self):
        """queue() should accept destination parameter."""
        service = MockDownloadService()
        result = service.queue(
            "peer1",
            [{"filename": "song.mp3", "size": 1000000}],
            destination="/music/discovery"
        )
        
        transfer_id = list(service.transfers.keys())[0]
        assert service.transfers[transfer_id].is_rec_download is True
    
    def test_get_status_returns_list(self):
        """get_status() should return list of Transfer objects."""
        service = MockDownloadService()
        service.queue("peer1", [{"filename": "song.mp3", "size": 1000000}])
        transfers = service.get_status()
        
        assert isinstance(transfers, list)
        assert len(transfers) == 1
        assert isinstance(transfers[0], Transfer)
        assert transfers[0].username == "peer1"
        assert transfers[0].filename == "song.mp3"
        assert transfers[0].state == "queued"
    
    def test_get_status_empty(self):
        """get_status() should return empty list when no transfers."""
        service = MockDownloadService()
        transfers = service.get_status()
        
        assert transfers == []
    
    def test_retry_success(self):
        """retry() should create new transfer on success."""
        service = MockDownloadService()
        service.queue("peer1", [{"filename": "song.mp3", "size": 1000000}])
        transfer_id = list(service.transfers.keys())[0]
        
        result = service.retry(transfer_id)
        
        assert result.success is True
        assert result.new_transfer_id is not None
        assert result.new_transfer_id != transfer_id
        assert len(service.transfers) == 2  # Old + new
    
    def test_retry_no_viable_peer(self):
        """retry() should return failure when no viable peer."""
        service = MockDownloadService()
        service.queue("nopeer1", [{"filename": "song.mp3", "size": 1000000}])
        transfer_id = list(service.transfers.keys())[0]
        
        result = service.retry(transfer_id)
        
        assert result.success is False
        assert "No viable peer" in result.message
    
    def test_retry_not_found(self):
        """retry() should raise error for unknown transfer_id."""
        service = MockDownloadService()
        
        with pytest.raises(ValueError, match="Transfer unknown not found"):
            service.retry("unknown")
    
    def test_cancel_success(self):
        """cancel() should mark transfer as cancelled."""
        service = MockDownloadService()
        service.queue("peer1", [{"filename": "song.mp3", "size": 1000000}])
        transfer_id = list(service.transfers.keys())[0]
        
        result = service.cancel(transfer_id)
        
        assert result is True
        assert service.transfers[transfer_id].state == "cancelled"
    
    def test_cancel_not_found(self):
        """cancel() should raise error for unknown transfer_id."""
        service = MockDownloadService()
        
        with pytest.raises(ValueError, match="Transfer unknown not found"):
            service.cancel("unknown")
    
    def test_get_transfer(self):
        """get_transfer() should return Transfer object."""
        service = MockDownloadService()
        service.queue("peer1", [{"filename": "song.mp3", "size": 1000000}])
        transfer_id = list(service.transfers.keys())[0]
        
        transfer = service.get_transfer(transfer_id)
        
        assert isinstance(transfer, Transfer)
        assert transfer.transfer_id == transfer_id
        assert transfer.username == "peer1"
        assert transfer.filename == "song.mp3"
        assert transfer.size == 1000000
        assert transfer.state == "queued"
    
    def test_get_transfer_not_found(self):
        """get_transfer() should raise error for unknown transfer_id."""
        service = MockDownloadService()
        
        with pytest.raises(ValueError, match="Transfer unknown not found"):
            service.get_transfer("unknown")


class TestQueueResultDataclass:
    """Test QueueResult dataclass."""
    
    def test_queue_result_creation(self):
        """QueueResult should be creatable with all fields."""
        result = QueueResult(
            enqueued_count=3,
            failures=[{"filename": "song.mp3", "message": "Peer offline"}],
            search_id="search-123"
        )
        
        assert result.enqueued_count == 3
        assert len(result.failures) == 1
        assert result.failures[0]["filename"] == "song.mp3"
        assert result.search_id == "search-123"
    
    def test_queue_result_without_search_id(self):
        """QueueResult should allow None for search_id."""
        result = QueueResult(
            enqueued_count=1,
            failures=[]
        )
        
        assert result.search_id is None


class TestTransferDataclass:
    """Test Transfer dataclass."""
    
    def test_transfer_creation(self):
        """Transfer should be creatable with all fields."""
        now = datetime.now()
        transfer = Transfer(
            transfer_id="transfer-123",
            username="peer1",
            filename="song.mp3",
            size=5242880,
            state="downloading",
            progress=45.5,
            speed=102400,
            started_at=now,
            completed_at=None,
            is_rec_download=False
        )
        
        assert transfer.transfer_id == "transfer-123"
        assert transfer.username == "peer1"
        assert transfer.filename == "song.mp3"
        assert transfer.size == 5242880
        assert transfer.state == "downloading"
        assert transfer.progress == 45.5
        assert transfer.speed == 102400
        assert transfer.started_at == now
        assert transfer.completed_at is None
        assert transfer.is_rec_download is False
    
    def test_transfer_with_completed_at(self):
        """Transfer should allow completed_at timestamp."""
        now = datetime.now()
        transfer = Transfer(
            transfer_id="transfer-123",
            username="peer1",
            filename="song.mp3",
            size=5242880,
            state="completed",
            progress=100.0,
            speed=0,
            started_at=now,
            completed_at=now,
            is_rec_download=True
        )
        
        assert transfer.state == "completed"
        assert transfer.progress == 100.0
        assert transfer.completed_at == now
        assert transfer.is_rec_download is True


class TestRetryResultDataclass:
    """Test RetryResult dataclass."""
    
    def test_retry_result_success(self):
        """RetryResult should represent successful retry."""
        result = RetryResult(
            success=True,
            message="Retrying from peer2",
            new_transfer_id="transfer-456"
        )
        
        assert result.success is True
        assert result.message == "Retrying from peer2"
        assert result.new_transfer_id == "transfer-456"
    
    def test_retry_result_failure(self):
        """RetryResult should represent failed retry."""
        result = RetryResult(
            success=False,
            message="No viable peer found"
        )
        
        assert result.success is False
        assert result.message == "No viable peer found"
        assert result.new_transfer_id is None
