"""
Unit tests for RecommendationService interface.
"""

import pytest
from typing import Optional

from app.services.interfaces.recommendation import (
    RecommendationService,
    Recommendation,
    Classification
)
from app.services.library import Song


class MockRecommendationService(RecommendationService):
    """Mock implementation for testing."""
    
    def __init__(self):
        self.mock_recs = []
        self.mock_library = []
    
    def fetch_recommendations(self, counts: dict[str, int]) -> list[Recommendation]:
        """Return mock recommendations based on counts."""
        recs = []
        
        for source, count in counts.items():
            for i in range(count):
                recs.append(Recommendation(
                    source=source,
                    artist=f"Artist {i+1}",
                    track=f"Track {i+1}",
                    mbid=f"mbid-{source}-{i+1}",
                    album=f"Album {i+1}"
                ))
        
        return recs
    
    def classify(self, recs: list[Recommendation], library: list) -> Classification:
        """Classify recs based on mock logic."""
        in_library = []
        to_download = []
        skipped = []
        
        for rec in recs:
            # Skip if no MBID
            if not rec.mbid:
                skipped.append(rec)
                continue
            
            # Check if in library (by MBID)
            if any(song.mbid == rec.mbid for song in library):
                in_library.append(rec)
            else:
                to_download.append(rec)
        
        return Classification(
            in_library=in_library,
            to_download=to_download,
            skipped=skipped
        )
    
    def queue_downloads(self, recs: list[Recommendation]) -> dict:
        """Mock queue operation."""
        queued = 0
        failed = 0
        failures = []
        
        for rec in recs:
            # Simulate failure for specific pattern
            if "fail" in rec.track.lower():
                failed += 1
                failures.append({
                    "artist": rec.artist,
                    "track": rec.track,
                    "message": "Simulated failure"
                })
            else:
                queued += 1
        
        return {
            "queued": queued,
            "failed": failed,
            "failures": failures
        }


class TestRecommendationServiceInterface:
    """Test RecommendationService interface contract."""
    
    def test_cannot_instantiate_abstract_class(self):
        """RecommendationService is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            RecommendationService()
    
    def test_concrete_implementation_must_implement_all_methods(self):
        """Concrete implementation must implement all abstract methods."""
        service = MockRecommendationService()
        assert isinstance(service, RecommendationService)
    
    def test_incomplete_implementation_raises_error(self):
        """Incomplete implementation should raise TypeError."""
        class IncompleteRecommendationService(RecommendationService):
            def fetch_recommendations(self, counts):
                pass
            # Missing other methods
        
        with pytest.raises(TypeError):
            IncompleteRecommendationService()


class TestRecommendationServiceMock:
    """Test mock implementation of RecommendationService."""
    
    def test_fetch_recommendations_single_source(self):
        """fetch_recommendations() should handle single source."""
        service = MockRecommendationService()
        recs = service.fetch_recommendations({"comfort_zone": 5})
        
        assert len(recs) == 5
        assert all(rec.source == "comfort_zone" for rec in recs)
        assert all(rec.artist.startswith("Artist") for rec in recs)
        assert all(rec.mbid.startswith("mbid-comfort_zone") for rec in recs)
    
    def test_fetch_recommendations_multiple_sources(self):
        """fetch_recommendations() should handle multiple sources."""
        service = MockRecommendationService()
        recs = service.fetch_recommendations({
            "comfort_zone": 3,
            "fresh_picks": 2,
            "deep_cuts": 1
        })
        
        assert len(recs) == 6
        comfort = [r for r in recs if r.source == "comfort_zone"]
        fresh = [r for r in recs if r.source == "fresh_picks"]
        deep = [r for r in recs if r.source == "deep_cuts"]
        
        assert len(comfort) == 3
        assert len(fresh) == 2
        assert len(deep) == 1
    
    def test_fetch_recommendations_empty_counts(self):
        """fetch_recommendations() should handle empty counts."""
        service = MockRecommendationService()
        recs = service.fetch_recommendations({})
        
        assert len(recs) == 0
    
    def test_classify_in_library(self):
        """classify() should identify recs already in library."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1"),
            Recommendation("comfort_zone", "Artist 2", "Track 2", "mbid-2")
        ]
        library = [
            Song("song-1", "Track 1", "Artist 1", "Album", "/path1", 180, 4320000, 192, 1, 2020, "Rock", 5, True, "mbid-1")
        ]
        
        classification = service.classify(recs, library)
        
        assert len(classification.in_library) == 1
        assert classification.in_library[0].track == "Track 1"
        assert len(classification.to_download) == 1
        assert classification.to_download[0].track == "Track 2"
        assert len(classification.skipped) == 0
    
    def test_classify_to_download(self):
        """classify() should identify recs not in library."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1"),
            Recommendation("comfort_zone", "Artist 2", "Track 2", "mbid-2")
        ]
        library = []  # Empty library
        
        classification = service.classify(recs, library)
        
        assert len(classification.in_library) == 0
        assert len(classification.to_download) == 2
        assert len(classification.skipped) == 0
    
    def test_classify_skipped_no_mbid(self):
        """classify() should skip recs without MBID."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", None),  # No MBID
            Recommendation("comfort_zone", "Artist 2", "Track 2", "mbid-2")
        ]
        library = []
        
        classification = service.classify(recs, library)
        
        assert len(classification.in_library) == 0
        assert len(classification.to_download) == 1
        assert len(classification.skipped) == 1
        assert classification.skipped[0].track == "Track 1"
    
    def test_classify_empty_recs(self):
        """classify() should handle empty recs list."""
        service = MockRecommendationService()
        classification = service.classify([], [])
        
        assert len(classification.in_library) == 0
        assert len(classification.to_download) == 0
        assert len(classification.skipped) == 0
    
    def test_queue_downloads_success(self):
        """queue_downloads() should queue all recs on success."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1"),
            Recommendation("comfort_zone", "Artist 2", "Track 2", "mbid-2")
        ]
        
        result = service.queue_downloads(recs)
        
        assert result["queued"] == 2
        assert result["failed"] == 0
        assert len(result["failures"]) == 0
    
    def test_queue_downloads_with_failures(self):
        """queue_downloads() should handle partial failures."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "Track 1", "mbid-1"),
            Recommendation("comfort_zone", "Artist 2", "fail_track", "mbid-2"),  # Will fail
            Recommendation("comfort_zone", "Artist 3", "Track 3", "mbid-3")
        ]
        
        result = service.queue_downloads(recs)
        
        assert result["queued"] == 2
        assert result["failed"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["track"] == "fail_track"
    
    def test_queue_downloads_all_fail(self):
        """queue_downloads() should handle all failures."""
        service = MockRecommendationService()
        recs = [
            Recommendation("comfort_zone", "Artist 1", "fail_track_1", "mbid-1"),
            Recommendation("comfort_zone", "Artist 2", "fail_track_2", "mbid-2")
        ]
        
        result = service.queue_downloads(recs)
        
        assert result["queued"] == 0
        assert result["failed"] == 2
        assert len(result["failures"]) == 2
    
    def test_queue_downloads_empty(self):
        """queue_downloads() should handle empty recs list."""
        service = MockRecommendationService()
        result = service.queue_downloads([])
        
        assert result["queued"] == 0
        assert result["failed"] == 0
        assert len(result["failures"]) == 0


class TestRecommendationDataclass:
    """Test Recommendation dataclass."""
    
    def test_recommendation_creation_full(self):
        """Recommendation should be creatable with all fields."""
        rec = Recommendation(
            source="comfort_zone",
            artist="Queen",
            track="Bohemian Rhapsody",
            mbid="612400e0-0c14-4f31-8e45-c98c8641b664",
            album="A Night at the Opera",
            release_mbid="release-123"
        )
        
        assert rec.source == "comfort_zone"
        assert rec.artist == "Queen"
        assert rec.track == "Bohemian Rhapsody"
        assert rec.mbid == "612400e0-0c14-4f31-8e45-c98c8641b664"
        assert rec.album == "A Night at the Opera"
        assert rec.release_mbid == "release-123"
    
    def test_recommendation_creation_minimal(self):
        """Recommendation should be creatable with required fields only."""
        rec = Recommendation(
            source="fresh_picks",
            artist="Artist",
            track="Track"
        )
        
        assert rec.source == "fresh_picks"
        assert rec.artist == "Artist"
        assert rec.track == "Track"
        assert rec.mbid is None
        assert rec.album is None
        assert rec.release_mbid is None
    
    def test_recommendation_sources(self):
        """Recommendation should support all source types."""
        sources = ["comfort_zone", "fresh_picks", "deep_cuts"]
        
        for source in sources:
            rec = Recommendation(source, "Artist", "Track")
            assert rec.source == source


class TestClassificationDataclass:
    """Test Classification dataclass."""
    
    def test_classification_creation(self):
        """Classification should be creatable with all fields."""
        rec1 = Recommendation("comfort_zone", "Artist 1", "Track 1")
        rec2 = Recommendation("comfort_zone", "Artist 2", "Track 2")
        rec3 = Recommendation("comfort_zone", "Artist 3", "Track 3")
        
        classification = Classification(
            in_library=[rec1],
            to_download=[rec2],
            skipped=[rec3]
        )
        
        assert len(classification.in_library) == 1
        assert len(classification.to_download) == 1
        assert len(classification.skipped) == 1
        assert classification.in_library[0].track == "Track 1"
        assert classification.to_download[0].track == "Track 2"
        assert classification.skipped[0].track == "Track 3"
    
    def test_classification_empty(self):
        """Classification should allow empty lists."""
        classification = Classification(
            in_library=[],
            to_download=[],
            skipped=[]
        )
        
        assert len(classification.in_library) == 0
        assert len(classification.to_download) == 0
        assert len(classification.skipped) == 0


class TestRecommendationWorkflow:
    """Integration-style tests for recommendation workflows."""
    
    def test_full_workflow(self):
        """Test complete workflow: fetch → classify → queue."""
        service = MockRecommendationService()
        
        # Fetch
        recs = service.fetch_recommendations({
            "comfort_zone": 3,
            "fresh_picks": 2
        })
        assert len(recs) == 5
        
        # Classify (with empty library, all go to download)
        classification = service.classify(recs, [])
        assert len(classification.to_download) == 5
        assert len(classification.in_library) == 0
        
        # Queue
        result = service.queue_downloads(classification.to_download)
        assert result["queued"] == 5
        assert result["failed"] == 0
    
    def test_workflow_with_library_dedupe(self):
        """Test workflow with library deduplication."""
        service = MockRecommendationService()
        
        # Fetch
        recs = service.fetch_recommendations({"comfort_zone": 3})
        
        # Create library with one matching rec
        library = [
            Song("song-1", recs[0].track, recs[0].artist, "Album", "/path", 180, 4320000, 192, 1, 2020, "Rock", 5, True, recs[0].mbid)
        ]
        
        # Classify
        classification = service.classify(recs, library)
        assert len(classification.in_library) == 1
        assert len(classification.to_download) == 2
        
        # Queue only to_download
        result = service.queue_downloads(classification.to_download)
        assert result["queued"] == 2
