"""
DownloadService — Abstract base class for download operations.

This interface defines the contract for download implementations (e.g., slskd).
All download services must implement these methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class QueueResult:
    """Result of a queue operation."""
    enqueued_count: int
    failures: list[dict]  # [{"filename": str, "message": str}, ...]
    search_id: Optional[str] = None


@dataclass
class Transfer:
    """Represents an active download transfer."""
    transfer_id: str
    username: str
    filename: str
    size: int
    state: str  # 'queued', 'downloading', 'completed', 'failed', 'cancelled'
    progress: float  # 0.0 to 100.0
    speed: Optional[int]  # bytes per second
    started_at: datetime
    completed_at: Optional[datetime]
    is_rec_download: bool = False
    fail_reason: Optional[str] = None
    """Raw slskd sub-state when state == 'failed' (e.g. 'timedout', 'errored',
    'rejected', 'aborted') — preserved so callers can tell a local
    connectivity blip (timedout/aborted) from genuine peer misbehavior
    (rejected/errored) instead of treating every failure identically."""


@dataclass
class RetryResult:
    """Result of a retry operation."""
    success: bool
    message: str
    new_transfer_id: Optional[str] = None


class DownloadService(ABC):
    """
    Abstract base class for download operations.
    
    Implementations:
    - SlskdDownload: Downloads via slskd (Soulseek)
    
    Usage:
        download_service = SlskdDownload(config)
        result = download_service.queue("peer1", [{"filename": "song.mp3", "size": 5242880}])
        transfers = download_service.get_status()
    """
    
    @abstractmethod
    def queue(
        self,
        username: str,
        files: list[dict],
        search_id: Optional[str] = None,
        destination: Optional[str] = None
    ) -> QueueResult:
        """
        Queue files for download from a peer.
        
        Args:
            username: Peer username
            files: List of file dicts with 'filename' and 'size' keys
            search_id: Optional search ID that originated these files
            destination: Optional destination directory override
            
        Returns:
            QueueResult with enqueued count and failures
            
        Raises:
            ServiceConnectionError: If cannot connect to download backend
            DownloadError: If queue operation fails entirely
        """
        pass
    
    @abstractmethod
    def get_status(self) -> list[Transfer]:
        """
        Get status of all active downloads.
        
        Returns:
            List of Transfer objects with current state
            
        Raises:
            ServiceConnectionError: If cannot connect to download backend
        """
        pass
    
    @abstractmethod
    def retry(self, transfer_id: str) -> RetryResult:
        """
        Retry a failed download from stored search results.
        
        Args:
            transfer_id: ID of failed transfer to retry
            
        Returns:
            RetryResult with success status and new transfer ID
            
        Raises:
            TransferNotFoundError: If transfer_id not found
            NoViablePeerError: If no alternative peer available
        """
        pass
    
    @abstractmethod
    def cancel(self, transfer_id: str) -> bool:
        """
        Cancel an active download.
        
        Args:
            transfer_id: ID of transfer to cancel
            
        Returns:
            True if cancelled successfully, False otherwise
            
        Raises:
            TransferNotFoundError: If transfer_id not found
        """
        pass
    
    @abstractmethod
    def delete_transfer(self, transfer_id: str) -> bool:
        """
        Permanently remove a transfer record from the download backend.

        Unlike cancel(), this asks the backend to forget the transfer
        entirely (used for finished transfers so they don't reappear on
        the next status poll).

        Args:
            transfer_id: ID of transfer to delete

        Returns:
            True if the backend confirmed removal, False otherwise
        """
        pass

    @abstractmethod
    def get_transfer(self, transfer_id: str) -> Transfer:
        """
        Get details of a specific transfer.
        
        Args:
            transfer_id: ID of transfer
            
        Returns:
            Transfer object with current state
            
        Raises:
            TransferNotFoundError: If transfer_id not found
        """
        pass
