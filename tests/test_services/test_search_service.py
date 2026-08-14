"""
Unit tests for SearchService interface.
"""

import pytest
from datetime import datetime, timezone
from typing import Optional

from app.services.interfaces.search import SearchService, SearchJob, SearchResult


class MockSearchService(SearchService):
    """Mock implementation for testing."""
    
    def __init__(self):
        self.searches = {}
        self.next_id = 1
    
    def search(self, query: str, artist: Optional[str] = None) -> SearchJob:
        search_id = f"search-{self.next_id}"
        self.next_id += 1
        job = SearchJob(
            search_id=search_id,
            query=query,
            artist=artist,
            created_at=datetime.now(),
            status="searching"
        )
        self.searches[search_id] = job
        return job
    
    def get_results(self, search_id: str) -> list[SearchResult]:
        if search_id not in self.searches:
            raise ValueError(f"Search {search_id} not found")
        
        # Return mock results
        return [
            SearchResult(
                username="peer1",
                filename="song.mp3",
                size=5242880,
                has_free_slot=True,
                upload_speed=102400,
                bitrate="320kbps",
                duration=240
            )
        ]
    
    def cancel(self, search_id: str) -> bool:
        if search_id not in self.searches:
            raise ValueError(f"Search {search_id} not found")
        self.searches[search_id].status = "cancelled"
        return True
    
    def get_status(self, search_id: str) -> SearchJob:
        if search_id not in self.searches:
            raise ValueError(f"Search {search_id} not found")
        return self.searches[search_id]
    
    def list_searches(self) -> list[SearchJob]:
        return sorted(
            self.searches.values(),
            key=lambda job: (job.created_at, job.search_id),
            reverse=True
        )

    def get_progress(self, search_id: str) -> dict:
        if search_id not in self.searches:
            raise ValueError(f"Search {search_id} not found")
        return {
            "response_count": 0,
            "file_count": 0,
            "is_complete": False,
            "elapsed_seconds": 0.0,
            "threshold": 10,
            "max_wait_seconds": 10,
        }


class TestSearchServiceInterface:
    """Test SearchService interface contract."""
    
    def test_cannot_instantiate_abstract_class(self):
        """SearchService is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SearchService()
    
    def test_concrete_implementation_must_implement_all_methods(self):
        """Concrete implementation must implement all abstract methods."""
        # This should work (all methods implemented)
        service = MockSearchService()
        assert isinstance(service, SearchService)
    
    def test_incomplete_implementation_raises_error(self):
        """Incomplete implementation should raise TypeError."""
        class IncompleteSearchService(SearchService):
            def search(self, query: str, artist: Optional[str] = None):
                pass
            # Missing other methods
        
        with pytest.raises(TypeError):
            IncompleteSearchService()


class TestSearchServiceMock:
    """Test mock implementation of SearchService."""
    
    def test_search_returns_job(self):
        """search() should return a SearchJob with correct metadata."""
        service = MockSearchService()
        job = service.search("Bohemian Rhapsody", artist="Queen")
        
        assert job.search_id == "search-1"
        assert job.query == "Bohemian Rhapsody"
        assert job.artist == "Queen"
        assert job.status == "searching"
        assert isinstance(job.created_at, datetime)
    
    def test_search_without_artist(self):
        """search() should work without artist parameter."""
        service = MockSearchService()
        job = service.search("Bohemian Rhapsody")
        
        assert job.query == "Bohemian Rhapsody"
        assert job.artist is None
    
    def test_get_results_returns_list(self):
        """get_results() should return list of SearchResult."""
        service = MockSearchService()
        job = service.search("Test")
        results = service.get_results(job.search_id)
        
        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].username == "peer1"
        assert results[0].filename == "song.mp3"
        assert results[0].has_free_slot is True
    
    def test_get_results_not_found_raises_error(self):
        """get_results() should raise error for unknown search_id."""
        service = MockSearchService()
        
        with pytest.raises(ValueError, match="Search unknown not found"):
            service.get_results("unknown")
    
    def test_cancel_returns_true(self):
        """cancel() should return True on success."""
        service = MockSearchService()
        job = service.search("Test")
        result = service.cancel(job.search_id)
        
        assert result is True
        assert service.searches[job.search_id].status == "cancelled"
    
    def test_cancel_not_found_raises_error(self):
        """cancel() should raise error for unknown search_id."""
        service = MockSearchService()
        
        with pytest.raises(ValueError, match="Search unknown not found"):
            service.cancel("unknown")
    
    def test_get_status_returns_job(self):
        """get_status() should return SearchJob with current status."""
        service = MockSearchService()
        job = service.search("Test")
        status = service.get_status(job.search_id)
        
        assert status.search_id == job.search_id
        assert status.status == "searching"
    
    def test_get_status_after_cancel(self):
        """get_status() should reflect cancelled status."""
        service = MockSearchService()
        job = service.search("Test")
        service.cancel(job.search_id)
        status = service.get_status(job.search_id)
        
        assert status.status == "cancelled"
    
    def test_list_searches_returns_all(self):
        """list_searches() should return all search jobs."""
        service = MockSearchService()
        service.search("First")
        service.search("Second")
        
        jobs = service.list_searches()
        
        assert len(jobs) == 2
        assert {job.query for job in jobs} == {"First", "Second"}
    
    def test_list_searches_empty(self):
        """list_searches() should return empty list when no searches."""
        service = MockSearchService()
        
        assert service.list_searches() == []
    
    def test_list_searches_newest_first(self):
        """list_searches() should return newest searches first."""
        service = MockSearchService()
        first = service.search("First")
        second = service.search("Second")
        
        jobs = service.list_searches()
        
        assert jobs[0].search_id == second.search_id
        assert jobs[1].search_id == first.search_id
    
    def test_list_searches_equal_timestamps_stable(self):
        """list_searches() should break ties deterministically by search_id."""
        service = MockSearchService()
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Two jobs with identical created_at (fast runs / mocked clocks)
        first = SearchJob(
            search_id="search-1",
            query="First",
            artist=None,
            created_at=now,
            status="searching",
        )
        second = SearchJob(
            search_id="search-2",
            query="Second",
            artist=None,
            created_at=now,
            status="searching",
        )
        service.searches = {"search-1": first, "search-2": second}
        
        jobs = service.list_searches()
        
        # Tie broken by search_id descending (search-2 > search-1)
        assert jobs[0].search_id == "search-2"
        assert jobs[1].search_id == "search-1"


class TestSearchJobDataclass:
    """Test SearchJob dataclass."""
    
    def test_search_job_creation(self):
        """SearchJob should be creatable with all fields."""
        job = SearchJob(
            search_id="test-123",
            query="Test Query",
            artist="Test Artist",
            created_at=datetime.now(),
            status="searching"
        )
        
        assert job.search_id == "test-123"
        assert job.query == "Test Query"
        assert job.artist == "Test Artist"
        assert job.status == "searching"
    
    def test_search_job_without_artist(self):
        """SearchJob should allow None for artist."""
        job = SearchJob(
            search_id="test-123",
            query="Test Query",
            artist=None,
            created_at=datetime.now(),
            status="searching"
        )
        
        assert job.artist is None


class TestSearchResultDataclass:
    """Test SearchResult dataclass."""
    
    def test_search_result_creation(self):
        """SearchResult should be creatable with all fields."""
        result = SearchResult(
            username="peer1",
            filename="song.mp3",
            size=5242880,
            has_free_slot=True,
            upload_speed=102400,
            bitrate="320kbps",
            duration=240
        )
        
        assert result.username == "peer1"
        assert result.filename == "song.mp3"
        assert result.size == 5242880
        assert result.has_free_slot is True
        assert result.upload_speed == 102400
        assert result.bitrate == "320kbps"
        assert result.duration == 240
    
    def test_search_result_with_optional_fields_none(self):
        """SearchResult should allow None for optional fields."""
        result = SearchResult(
            username="peer1",
            filename="song.mp3",
            size=5242880,
            has_free_slot=False,
            upload_speed=None,
            bitrate=None,
            duration=None
        )
        
        assert result.upload_speed is None
        assert result.bitrate is None
        assert result.duration is None
