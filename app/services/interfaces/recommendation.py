"""
RecommendationService — Abstract base class for recommendation operations.

This interface defines the contract for recommendation implementations (e.g., ListenBrainz).
All recommendation services must implement these methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Recommendation:
    """Represents a single recommendation."""
    source: str  # 'comfort_zone', 'fresh_picks', 'deep_cuts'
    artist: str
    track: str
    mbid: Optional[str] = None  # MusicBrainz recording ID
    album: Optional[str] = None
    release_mbid: Optional[str] = None


@dataclass
class Classification:
    """Result of classifying recommendations against library."""
    in_library: list[Recommendation]  # Already in library, add to playlist
    to_download: list[Recommendation]  # Not in library, search slskd
    skipped: list[Recommendation]  # Skipped (e.g., no MBID, invalid)


class RecommendationService(ABC):
    """
    Abstract base class for recommendation operations.
    
    Implementations:
    - ListenBrainzRecs: Fetches recommendations from ListenBrainz API
    
    Usage:
        rec_service = ListenBrainzRecs(config)
        recs = rec_service.fetch_recommendations({"comfort_zone": 5, "fresh_picks": 5})
        classification = rec_service.classify(recs, library_songs)
        result = rec_service.queue_downloads(classification.to_download)
    """
    
    @abstractmethod
    def fetch_recommendations(self, counts: dict[str, int]) -> list[Recommendation]:
        """
        Fetch recommendations from external source.
        
        Args:
            counts: Dict mapping source name to count, e.g.:
                    {"comfort_zone": 5, "fresh_picks": 5, "deep_cuts": 5}
            
        Returns:
            List of Recommendation objects
            
        Raises:
            ServiceConnectionError: If cannot connect to recommendation backend
            RecommendationError: If fetch fails
        """
        pass
    
    @abstractmethod
    def classify(self, recs: list[Recommendation], library: list) -> Classification:
        """
        Classify recommendations as in_library, to_download, or skipped.
        
        Args:
            recs: List of Recommendation objects to classify
            library: List of Song objects from library (for deduplication)
            
        Returns:
            Classification with in_library, to_download, and skipped lists
            
        Matching strategy (in order):
        1. Exact MusicBrainz recording MBID match
        2. Normalized artist + track name match
        3. Normalized filename match
        
        Raises:
            ClassificationError: If classification fails
        """
        pass
    
    @abstractmethod
    def queue_downloads(self, recs: list[Recommendation]) -> dict:
        """
        Search and queue downloads for missing recommendations.
        
        Args:
            recs: List of Recommendation objects to download
            
        Returns:
            Dict with results:
            {
                "queued": int,
                "failed": int,
                "failures": [{"artist": str, "track": str, "message": str}]
            }
            
        Process:
        1. For each rec: search slskd with track name only
        2. Post-filter by artist words
        3. Pick peer with artist match + free slot
        4. Store search responses for retry
        5. Queue download
        
        Raises:
            ServiceConnectionError: If cannot connect to download backend
            DownloadError: If queue operation fails entirely
        """
        pass
