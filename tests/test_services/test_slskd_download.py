"""
Unit tests for SlskdDownload implementation.
"""

import urllib.parse

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from app.services.download import SlskdDownload
from app.services.interfaces.download import QueueResult, Transfer, RetryResult
from app.exceptions import (
    TransferNotFoundError,
    NoViablePeerError,
    QueueError,
    SlskdConnectionError,
    MaxRetriesExceededError
)
from app.config import Config


class MockConfig:
    """Mock config for testing."""
    
    class SlskdConfig:
        url = "http://slskd:5030"
        api_key = "test-api-key"
    
    class DownloadConfig:
        max_retries_per_track = 3
        bad_peer_threshold = 1
    
    slskd = SlskdConfig()
    download = DownloadConfig()


class TestSlskdDownloadInit:
    """Test SlskdDownload initialization."""
    
    def test_init_with_config(self):
        """SlskdDownload should initialize with config."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        assert download.base_url == "http://slskd:5030"
        assert download.api_key == "test-api-key"
        assert isinstance(download._transfers, dict)
        assert isinstance(download._blocked_peers, set)
    
    def test_allowed_extensions(self):
        """SlskdDownload should have correct allowed extensions."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        assert ".mp3" in download.allowed_extensions
        assert ".flac" in download.allowed_extensions
        assert ".m4a" in download.allowed_extensions


class TestSlskdQueueMethod:
    """Test queue() method."""
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_success_all(self, mock_post):
        """queue() should enqueue all files successfully."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [
            {"filename": "song1.mp3", "size": 5242880},
            {"filename": "song2.mp3", "size": 6291456}
        ]
        
        result = download.queue("peer1", files)
        
        assert isinstance(result, QueueResult)
        assert result.enqueued_count == 2
        assert len(result.failures) == 0
        assert len(download._transfers) == 2
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_partial_success(self, mock_post):
        """queue() should handle partial success."""
        mock_response = Mock()
        mock_response.status_code = 207
        mock_response.json.return_value = {
            "failures": [{"filename": "song2.mp3", "message": "Peer offline"}]
        }
        mock_post.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [
            {"filename": "song1.mp3", "size": 5242880},
            {"filename": "song2.mp3", "size": 6291456}
        ]
        
        result = download.queue("peer1", files)
        
        assert result.enqueued_count == 1
        assert len(result.failures) == 1
        assert result.failures[0]["filename"] == "song2.mp3"
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_complete_failure(self, mock_post):
        """queue() should raise QueueError on complete failure."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [{"filename": "song.mp3", "size": 5242880}]
        
        with pytest.raises(QueueError):
            download.queue("peer1", files)
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_with_search_id(self, mock_post):
        """queue() should include search_id in payload."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [{"filename": "song.mp3", "size": 5242880}]
        result = download.queue("peer1", files, search_id="search-123")
        
        assert result.search_id == "search-123"
        
        # Verify payload included searchId
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["searchId"] == "search-123"
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_with_destination(self, mock_post):
        """queue() should include destination in payload."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [{"filename": "song.mp3", "size": 5242880}]
        result = download.queue("peer1", files, destination="/music/discovery")
        
        # Verify payload included destination
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["options"]["destination"] == "/music/discovery"
        
        # Verify transfer marked as rec download
        transfer = list(download._transfers.values())[0]
        assert transfer.is_rec_download is True
    
    @patch('app.services.download.requests.Session.post')
    def test_queue_connection_error(self, mock_post):
        """queue() should raise SlskdConnectionError on connection error."""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        files = [{"filename": "song.mp3", "size": 5242880}]
        
        with pytest.raises(SlskdConnectionError):
            download.queue("peer1", files)


class TestSlskdGetStatusMethod:
    """Test get_status() method."""
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_list_format(self, mock_get):
        """get_status() should parse list format response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {
                        "files": [
                            {
                                "id": "transfer-1",
                                "filename": "song.mp3",
                                "size": 5242880,
                                "state": "Downloading",
                                "bytesTransferred": 2621440,
                                "averageSpeed": 102400
                            }
                        ]
                    }
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        transfers = download.get_status()
        
        assert len(transfers) == 1
        assert transfers[0].username == "peer1"
        assert transfers[0].filename == "song.mp3"
        assert transfers[0].state == "downloading"
        assert transfers[0].progress == 50.0
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_dict_format(self, mock_get):
        """get_status() should parse dict format response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "peer1": [
                {
                    "id": "transfer-1",
                    "filename": "song.mp3",
                    "size": 5242880,
                    "state": "Completed, Succeeded",
                    "bytesTransferred": 5242880
                }
            ]
        }
        mock_get.return_value = mock_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        transfers = download.get_status()
        
        assert len(transfers) == 1
        assert transfers[0].state == "completed"
        assert transfers[0].progress == 100.0
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_inprogress_maps_to_downloading(self, mock_get):
        """Real slskd 'InProgress' state should map to downloading."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "InProgress", "bytesTransferred": 50}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "downloading"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_queued_locally_maps_to_queued(self, mock_get):
        """Real slskd 'Queued, Locally' state should map to queued (not downloading)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "Queued, Locally", "bytesTransferred": 0}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "queued"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_queued_remotely_maps_to_queued(self, mock_get):
        """Real slskd 'Queued, Remotely' state should map to queued."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "Queued, Remotely", "bytesTransferred": 0}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "queued"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_completed_cancelled_maps_to_cancelled(self, mock_get):
        """Real slskd 'Completed, Cancelled' state should map to cancelled."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "Completed, Cancelled", "bytesTransferred": 0}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "cancelled"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_completed_errored_maps_to_failed(self, mock_get):
        """Real slskd 'Completed, Errored' state should map to failed."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "Completed, Errored", "bytesTransferred": 0}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "failed"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_completed_timedout_maps_to_failed(self, mock_get):
        """Real slskd 'Completed, TimedOut' state should map to failed."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "username": "peer1",
                "directories": [
                    {"files": [
                        {"id": "t1", "filename": "song.mp3", "size": 100,
                         "state": "Completed, TimedOut", "bytesTransferred": 0}
                    ]}
                ]
            }
        ]
        mock_get.return_value = mock_response
        
        transfers = SlskdDownload(MockConfig()).get_status()
        
        assert transfers[0].state == "failed"
    
    @patch('app.services.download.requests.Session.get')
    def test_get_status_connection_error(self, mock_get):
        """get_status() should raise SlskdConnectionError on connection error."""
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        with pytest.raises(SlskdConnectionError):
            download.get_status()


class TestSlskdRetryMethod:
    """Test retry() method."""
    
    @patch('app.services.download.requests.Session.post')
    @patch('app.services.download.requests.Session.delete')
    def test_retry_success(self, mock_delete, mock_post):
        """retry() should successfully retry from alternative peer."""
        # Mock queue responses
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        mock_delete_response = Mock()
        mock_delete_response.status_code = 200
        mock_delete.return_value = mock_delete_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create initial transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # Store search responses with alternative peer
        tracker_key = "peer1:song.mp3"
        download.store_search_responses(tracker_key, [
            {
                "username": "peer2",
                "files": [{"filename": "song.mp3", "size": 5242880}],
                "hasFreeUploadSlot": True
            }
        ])
        
        # Retry
        result = download.retry(transfer_id)
        
        assert isinstance(result, RetryResult)
        assert result.success is True
        assert "peer2" in result.message
        assert result.new_transfer_id is not None
    
    def test_retry_not_found(self):
        """retry() should raise TransferNotFoundError for unknown ID."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        with pytest.raises(TransferNotFoundError):
            download.retry("nonexistent")
    
    @patch('app.services.download.requests.Session.post')
    def test_retry_max_retries_exceeded(self, mock_post):
        """retry() should raise MaxRetriesExceededError when limit reached."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create initial transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # Set retry count to max
        tracker_key = "peer1:song.mp3"
        download._retry_counts[tracker_key] = 3
        
        with pytest.raises(MaxRetriesExceededError):
            download.retry(transfer_id)
    
    @patch('app.services.download.requests.Session.post')
    def test_retry_no_stored_responses(self, mock_post):
        """retry() should raise NoViablePeerError when no stored responses."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create initial transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # No stored responses
        with pytest.raises(NoViablePeerError):
            download.retry(transfer_id)
    
    @patch('app.services.download.requests.Session.post')
    def test_retry_no_viable_peer(self, mock_post):
        """retry() should raise NoViablePeerError when no peer with free slot."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create initial transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # Store responses but all peers busy
        tracker_key = "peer1:song.mp3"
        download.store_search_responses(tracker_key, [
            {
                "username": "peer2",
                "files": [{"filename": "song.mp3", "size": 5242880}],
                "hasFreeUploadSlot": False  # No free slot
            }
        ])
        
        with pytest.raises(NoViablePeerError):
            download.retry(transfer_id)


class TestSlskdCancelMethod:
    """Test cancel() method."""
    
    @patch('app.services.download.requests.Session.delete')
    @patch('app.services.download.requests.Session.post')
    def test_cancel_success(self, mock_post, mock_delete):
        """cancel() should cancel transfer and return True."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        mock_delete_response = Mock()
        mock_delete_response.status_code = 200
        mock_delete.return_value = mock_delete_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # Cancel
        result = download.cancel(transfer_id)
        
        assert result is True
        assert download._transfers[transfer_id].state == "cancelled"
    
    def test_cancel_not_found(self):
        """cancel() should raise TransferNotFoundError for unknown ID."""
        config = MockConfig()
        download = SlskdDownload(config)

        with pytest.raises(TransferNotFoundError):
            download.cancel("nonexistent")


class TestSlskdDeleteTransferMethod:
    """Test delete_transfer() method."""

    @patch('app.services.download.requests.Session.delete')
    @patch('app.services.download.requests.Session.post')
    def test_delete_success_uses_remove_true(self, mock_post, mock_delete):
        """delete_transfer() should call DELETE with ?remove=true and drop the entry."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response

        mock_delete_response = Mock()
        mock_delete_response.status_code = 200
        mock_delete.return_value = mock_delete_response

        config = MockConfig()
        download = SlskdDownload(config)

        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]

        result = download.delete_transfer(transfer_id)

        assert result is True
        assert transfer_id not in download._transfers
        called_url = mock_delete.call_args[0][0]
        assert called_url.endswith("?remove=true")

        # The URL must use the parsed slskd download GUID (the timestamp
        # segment of "username:filename:timestamp"), not the raw compound
        # transfer_id — sending the whole compound string as the id caused
        # slskd to fall back to removing unrelated queued/downloading
        # transfers instead of just this one.
        download_id = SlskdDownload._extract_download_id(transfer_id)
        assert f"/{download_id}?remove=true" in called_url
        assert transfer_id != download_id  # sanity: id extraction actually changed something
        assert urllib.parse.quote(transfer_id) not in called_url

    @patch('app.services.download.requests.Session.delete')
    @patch('app.services.download.requests.Session.post')
    def test_delete_http_failure_keeps_entry(self, mock_post, mock_delete):
        """delete_transfer() should return False and keep the entry on non-2xx."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response

        mock_delete_response = Mock()
        mock_delete_response.status_code = 500
        mock_delete.return_value = mock_delete_response

        config = MockConfig()
        download = SlskdDownload(config)

        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]

        result = download.delete_transfer(transfer_id)

        assert result is False
        assert transfer_id in download._transfers

    def test_delete_unknown_transfer_returns_false(self):
        """delete_transfer() should return False (not raise) for an unknown ID."""
        config = MockConfig()
        download = SlskdDownload(config)

        assert download.delete_transfer("nonexistent") is False


class TestSlskdGetTransferMethod:
    """Test get_transfer() method."""
    
    @patch('app.services.download.requests.Session.post')
    def test_get_transfer_success(self, mock_post):
        """get_transfer() should return transfer details."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post.return_value = mock_post_response
        
        config = MockConfig()
        download = SlskdDownload(config)
        
        # Create transfer
        files = [{"filename": "song.mp3", "size": 5242880}]
        download.queue("peer1", files)
        transfer_id = list(download._transfers.keys())[0]
        
        # Get transfer
        transfer = download.get_transfer(transfer_id)
        
        assert isinstance(transfer, Transfer)
        assert transfer.transfer_id == transfer_id
        assert transfer.username == "peer1"
        assert transfer.filename == "song.mp3"
    
    def test_get_transfer_not_found(self):
        """get_transfer() should raise TransferNotFoundError for unknown ID."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        with pytest.raises(TransferNotFoundError):
            download.get_transfer("nonexistent")


class TestSlskdPeerManagement:
    """Test peer management methods."""
    
    def test_mark_peer_bad(self):
        """mark_peer_bad() should increment failure count."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        download.mark_peer_bad("peer1")
        
        assert download._bad_peers["peer1"] == 1
        assert "peer1" in download._blocked_peers  # Threshold is 1
    
    def test_mark_peer_bad_multiple(self):
        """mark_peer_bad() should accumulate failures."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        download.mark_peer_bad("peer1")
        download.mark_peer_bad("peer1")
        
        assert download._bad_peers["peer1"] == 2
    
    def test_unblock_peer(self):
        """unblock_peer() should remove from blocked set."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        download.mark_peer_bad("peer1")
        assert "peer1" in download._blocked_peers
        
        download.unblock_peer("peer1")
        assert "peer1" not in download._blocked_peers
        assert "peer1" not in download._bad_peers
    
    def test_store_search_responses(self):
        """store_search_responses() should store responses for retry."""
        config = MockConfig()
        download = SlskdDownload(config)
        
        responses = [
            {"username": "peer1", "files": [{"filename": "song.mp3"}]}
        ]
        
        download.store_search_responses("peer1:song.mp3", responses)
        
        assert "peer1:song.mp3" in download._search_responses
        assert len(download._search_responses["peer1:song.mp3"]) == 1
