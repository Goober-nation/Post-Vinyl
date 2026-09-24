"""
FeedbackService — Abstract base class for feedback operations.

This interface defines the contract for feedback implementations (e.g., ListenBrainz).
All feedback services must implement these methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SyncResult:
    """Result of a sync operation."""
    synced_count: int
    failed_count: int
    failures: list[dict]  # [{"song_id": str, "mbid": str, "message": str}, ...]


class FeedbackService(ABC):
    """
    Abstract base class for feedback operations.
    
    Implementations:
    - ListenBrainzFeedback: Submits feedback to ListenBrainz API
    
    Usage:
        feedback_service = ListenBrainzFeedback(config)
        feedback_service.send_feedback("mbid-123", 1)  # Love
        feedback_service.send_feedback("mbid-456", -1)  # Hate
        
        result = feedback_service.sync_loves(starred_songs)
        result = feedback_service.sync_hates(trashed_songs)
    """
    
    @abstractmethod
    def send_feedback(self, mbid: str, score: int) -> bool:
        """
        Send love (+1) or hate (-1) feedback for a recording.
        
        Args:
            mbid: MusicBrainz recording ID
            score: +1 for love, -1 for hate
            
        Returns:
            True if successful, False otherwise
            
        Raises:
            ServiceConnectionError: If cannot connect to feedback backend
            FeedbackError: If submission fails
            ValueError: If score not in (+1, -1)
        """
        pass
    
    @abstractmethod
    def sync_loves(self, starred: list) -> SyncResult:
        """
        Sync Navidrome starred songs to feedback service as loves.
        
        Args:
            starred: List of Song objects (from LibraryService.get_starred())
            
        Returns:
            SyncResult with synced count and failures
            
        Process:
        1. For each starred song with MBID
        2. Send +1 feedback
        3. Track successes and failures
        
        Raises:
            ServiceConnectionError: If cannot connect to feedback backend
        """
        pass
    
    @abstractmethod
    def sync_hates(self, trashed: list) -> SyncResult:
        """
        Sync Navidrome trash playlist to feedback service as hates.
        
        Args:
            trashed: List of Song objects (from Trash playlist)
            
        Returns:
            SyncResult with synced count and failures
            
        Process:
        1. For each trashed song with MBID
        2. Send -1 feedback
        3. Track successes and failures
        
        Raises:
            ServiceConnectionError: If cannot connect to feedback backend
        """
        pass
