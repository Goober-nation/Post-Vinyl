"""
SearchService — Abstract base class for search operations.

This interface defines the contract for search implementations (e.g., slskd).
All search services must implement these methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SearchJob:
    """Represents an initiated search job."""
    search_id: str
    query: str
    artist: Optional[str]
    created_at: datetime
    status: str  # 'searching', 'completed', 'failed', 'cancelled'


@dataclass
class SearchResult:
    """Represents a single search result (peer response)."""
    username: str
    filename: str
    size: int
    has_free_slot: bool
    upload_speed: Optional[int]
    bitrate: Optional[str]
    duration: Optional[int]


class SearchService(ABC):
    """
    Abstract base class for search operations.
    
    Implementations:
    - SlskdSearch: Searches via slskd (Soulseek)
    
    Usage:
        search_service = SlskdSearch(config)
        job = search_service.search("Bohemian Rhapsody", artist="Queen")
        results = search_service.get_results(job.search_id)
    """
    
    @abstractmethod
    def search(self, query: str, artist: Optional[str] = None) -> SearchJob:
        """
        Initiate a search.
        
        Args:
            query: Track or album name to search for
            artist: Optional artist name for post-filtering
            
        Returns:
            SearchJob with search_id and metadata
            
        Raises:
            ServiceConnectionError: If cannot connect to search backend
            SearchError: If search initiation fails
        """
        pass
    
    @abstractmethod
    def get_results(self, search_id: str) -> list[SearchResult]:
        """
        Fetch search results.
        
        Args:
            search_id: ID from SearchJob
            
        Returns:
            List of SearchResult objects (peer responses)
            
        Raises:
            SearchNotFoundError: If search_id not found
            ServiceConnectionError: If cannot connect to search backend
        """
        pass
    
    @abstractmethod
    def cancel(self, search_id: str) -> bool:
        """
        Cancel an in-progress search.
        
        Args:
            search_id: ID from SearchJob
            
        Returns:
            True if cancelled successfully, False otherwise
            
        Raises:
            SearchNotFoundError: If search_id not found
        """
        pass
    
    @abstractmethod
    def get_status(self, search_id: str) -> SearchJob:
        """
        Get current status of a search job.
        
        Args:
            search_id: ID from SearchJob
            
        Returns:
            SearchJob with updated status
            
        Raises:
            SearchNotFoundError: If search_id not found
        """
        pass
    
    @abstractmethod
    def list_searches(self) -> list[SearchJob]:
        """
        List all search jobs, newest first (ties broken by search_id).

        Returns:
            List of SearchJob objects
        """
        pass

    @abstractmethod
    def get_progress(self, search_id: str) -> dict:
        """
        Peek at a search's live progress without driving it to completion.

        Unlike get_results(), this never cancels the search to flush
        results — it's meant to be polled repeatedly while a search is
        still running.

        Args:
            search_id: ID from SearchJob

        Returns:
            Dict with response_count, file_count, is_complete,
            elapsed_seconds, threshold, max_wait_seconds

        Raises:
            SearchNotFoundError: If search_id not found
        """
        pass
