"""
Unit tests for SlskdSearch implementation.
"""

import time

import pytest
import requests
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from app.services.search import SlskdSearch
from app.services.interfaces.search import SearchJob, SearchResult
from app.exceptions import (
    SearchNotFoundError,
    SearchInitiationError,
    SearchRateLimitedError,
    SlskdConnectionError
)
from app.config import Config


class MockConfig:
    """Mock config for testing."""
    
    class SlskdConfig:
        url = "http://slskd:5030"
        api_key = "test-api-key"
    
    class SearchConfig:
        response_threshold = 10
        response_cap = 250
        min_wait_seconds = 3
        wait_seconds = 10
        poll_interval = 1
        response_limit = 60
        # Generous on purpose: existing tests in this file call search()
        # repeatedly with no pacing of their own, and aren't testing rate
        # limiting. TestSlskdRateLimiting/TestSlskdQueryCache below build
        # their own tightly-configured instances instead of relying on this.
        rate_limit_max_searches = 1000
        rate_limit_window_seconds = 1
        rate_limit_wait_timeout_seconds = 5
        query_cache_ttl_seconds = 0  # disabled by default so a fresh
        # search() always hits the mocked POST; cache behavior is opt-in
        # per-test via a dedicated config instance.

    slskd = SlskdConfig()
    search = SearchConfig()


class TestSlskdSearchInit:
    """Test SlskdSearch initialization."""
    
    def test_init_with_config(self):
        """SlskdSearch should initialize with config."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        assert search.base_url == "http://slskd:5030"
        assert search.api_key == "test-api-key"
        assert isinstance(search._searches, dict)
        assert isinstance(search._responses, dict)
    
    def test_get_headers_with_api_key(self):
        """_get_headers should include API key."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        headers = search._get_headers()
        
        assert headers["Content-Type"] == "application/json"
        assert headers["X-API-Key"] == "test-api-key"
    
    def test_get_headers_without_api_key(self):
        """_get_headers should work without API key."""
        config = MockConfig()
        config.slskd.api_key = ""
        search = SlskdSearch(config)
        
        headers = search._get_headers()
        
        assert headers["Content-Type"] == "application/json"
        assert "X-API-Key" not in headers


class TestSlskdSearchMethod:
    """Test search() method."""
    
    @patch('app.services.search.requests.Session.post')
    def test_search_success(self, mock_post):
        """search() should initiate search and return SearchJob."""
        # Mock response
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Bohemian Rhapsody", artist="Queen")
        
        assert isinstance(job, SearchJob)
        assert job.search_id == "search-123"
        assert job.query == "Bohemian Rhapsody"
        assert job.artist == "Queen"
        assert job.status == "searching"
        
        # Verify API call
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "/api/v0/searches" in call_args[0][0]
        assert call_args[1]["json"] == {
            "searchText": "Bohemian Rhapsody",
            "responseLimit": 60,
        }
    
    @patch('app.services.search.requests.Session.post')
    def test_search_without_artist(self, mock_post):
        """search() should work without artist parameter."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Bohemian Rhapsody")
        
        assert job.artist is None
    
    @patch('app.services.search.requests.Session.post')
    def test_search_failure_http_error(self, mock_post):
        """search() should raise SearchInitiationError on HTTP error."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SearchInitiationError):
            search.search("Test")
    
    @patch('app.services.search.requests.Session.post')
    def test_search_failure_no_id(self, mock_post):
        """search() should raise SearchInitiationError if no ID returned."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {}  # No ID
        mock_post.return_value = mock_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SearchInitiationError):
            search.search("Test")
    
    @patch('app.services.search.requests.Session.post')
    def test_search_failure_connection_error(self, mock_post):
        """search() should raise SlskdConnectionError on connection error."""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SlskdConnectionError):
            search.search("Test")


class TestSlskdGetResultsMethod:
    """Test get_results() method."""
    
    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_results_success(self, mock_post, mock_get):
        """get_results() should fetch and return results."""
        # Mock search initiation
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        # Mock metadata fetch (completed)
        mock_meta_response = Mock()
        mock_meta_response.status_code = 200
        mock_meta_response.json.return_value = {
            "id": "search-123",
            "isComplete": True,
            "responseCount": 5,
            "fileCount": 10
        }
        
        # Mock responses fetch
        mock_responses_response = Mock()
        mock_responses_response.status_code = 200
        mock_responses_response.json.return_value = [
            {
                "username": "peer1",
                "files": [{"filename": "song.mp3", "size": 5242880, "bitRate": 320, "duration": 240}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400
            }
        ]
        
        mock_get.side_effect = [mock_meta_response, mock_responses_response]
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        # Initiate search
        job = search.search("Test")
        
        # Get results
        results = search.get_results(job.search_id)
        
        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].username == "peer1"
        assert results[0].filename == "song.mp3"
    
    def test_get_results_not_found(self):
        """get_results() should raise SearchNotFoundError for unknown ID."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SearchNotFoundError):
            search.get_results("nonexistent")
    
    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_results_with_artist_filter(self, mock_post, mock_get):
        """get_results() should apply artist filter when specified."""
        # Mock search initiation
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        # Mock metadata
        mock_meta_response = Mock()
        mock_meta_response.status_code = 200
        mock_meta_response.json.return_value = {
            "id": "search-123",
            "isComplete": True,
            "responseCount": 3,
            "fileCount": 6
        }
        
        # Mock responses (some with artist, some without)
        mock_responses_response = Mock()
        mock_responses_response.status_code = 200
        mock_responses_response.json.return_value = [
            {
                "username": "peer1",
                "files": [{"filename": "Queen - Bohemian Rhapsody.mp3", "size": 5242880}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400
            },
            {
                "username": "peer2",
                "files": [{"filename": "Bohemian Rhapsody.mp3", "size": 5242880}],  # No artist
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400
            },
            {
                "username": "peer3",
                "files": [{"filename": "queen_bohemian_rhapsody.mp3", "size": 5242880}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400
            }
        ]
        
        mock_get.side_effect = [mock_meta_response, mock_responses_response]
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        # Initiate search with artist
        job = search.search("Bohemian Rhapsody", artist="Queen")
        
        # Get results
        results = search.get_results(job.search_id)
        
        # Should filter to only Queen tracks
        assert len(results) == 2
        assert all("queen" in r.filename.lower() for r in results)


class TestSlskdCancelMethod:
    """Test cancel() method."""
    
    @patch('app.services.search.requests.Session.put')
    @patch('app.services.search.requests.Session.post')
    def test_cancel_success(self, mock_post, mock_put):
        """cancel() should cancel search and return True."""
        # Mock search initiation
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        # Mock cancel
        mock_put_response = Mock()
        mock_put_response.status_code = 200
        mock_put.return_value = mock_put_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Test")
        result = search.cancel(job.search_id)
        
        assert result is True
        assert search._searches[job.search_id].status == "cancelled"
    
    def test_cancel_not_found(self):
        """cancel() should raise SearchNotFoundError for unknown ID."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SearchNotFoundError):
            search.cancel("nonexistent")
    
    @patch('app.services.search.requests.Session.put')
    @patch('app.services.search.requests.Session.post')
    def test_cancel_failure(self, mock_post, mock_put):
        """cancel() should return False on failure."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        mock_put_response = Mock()
        mock_put_response.status_code = 500
        mock_put.return_value = mock_put_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Test")
        result = search.cancel(job.search_id)
        
        assert result is False


class TestSlskdGetStatusMethod:
    """Test get_status() method."""
    
    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_status_searching(self, mock_post, mock_get):
        """get_status() should return searching status."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "id": "search-123",
            "isComplete": False
        }
        mock_get.return_value = mock_get_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Test")
        status = search.get_status(job.search_id)
        
        assert status.status == "searching"
    
    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_status_completed(self, mock_post, mock_get):
        """get_status() should return completed status."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response
        
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "id": "search-123",
            "isComplete": True
        }
        mock_get.return_value = mock_get_response
        
        config = MockConfig()
        search = SlskdSearch(config)
        
        job = search.search("Test")
        status = search.get_status(job.search_id)
        
        assert status.status == "completed"
    
    def test_get_status_not_found(self):
        """get_status() should raise SearchNotFoundError for unknown ID."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        with pytest.raises(SearchNotFoundError):
            search.get_status("nonexistent")


class TestSlskdGetProgressMethod:
    """Test get_progress() method."""

    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_progress_searching(self, mock_post, mock_get):
        """get_progress() should peek at live counts without cancelling."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response

        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {
            "id": "search-123",
            "isComplete": False,
            "responseCount": 4,
            "fileCount": 9,
        }
        mock_get.return_value = mock_get_response

        config = MockConfig()
        search = SlskdSearch(config)

        job = search.search("Test")
        progress = search.get_progress(job.search_id)

        assert progress["response_count"] == 4
        assert progress["file_count"] == 9
        assert progress["is_complete"] is False
        assert progress["threshold"] == config.search.response_threshold
        assert progress["max_wait_seconds"] == config.search.wait_seconds
        assert progress["elapsed_seconds"] >= 0

        # No cancel/PUT should have been issued by a plain progress peek.
        with patch('app.services.search.requests.Session.put') as mock_put:
            search.get_progress(job.search_id)
            mock_put.assert_not_called()

    @patch("app.services.search.requests.Session.put")
    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_get_progress_cancels_at_response_cap(self, mock_post, mock_get, mock_put):
        """The live progress poll cancels a search when the response cap is reached."""
        mock_post_response = Mock(status_code=201)
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response

        mock_get_response = Mock(status_code=200)
        mock_get_response.json.return_value = {
            "id": "search-123",
            "isComplete": False,
            "responseCount": 250,
            "fileCount": 900,
        }
        mock_get.return_value = mock_get_response
        mock_put.return_value = Mock(status_code=200)

        search = SlskdSearch(MockConfig())
        job = search.search("Test")

        progress = search.get_progress(job.search_id)

        assert progress["response_count"] == 250
        assert progress["file_count"] == 900
        assert progress["is_complete"] is True
        assert progress["response_cap"] == 250
        assert progress["stop_reason"] == "response_cap"
        mock_put.assert_called_once()

        mock_get.reset_mock()
        progress = search.get_progress(job.search_id)
        assert progress["response_count"] == 250
        assert progress["stop_reason"] == "response_cap"
        mock_get.assert_not_called()

    def test_get_progress_not_found(self):
        """get_progress() should raise SearchNotFoundError for unknown ID."""
        config = MockConfig()
        search = SlskdSearch(config)

        with pytest.raises(SearchNotFoundError):
            search.get_progress("nonexistent")

    @patch('app.services.search.requests.Session.get')
    @patch('app.services.search.requests.Session.post')
    def test_get_progress_after_completion(self, mock_post, mock_get):
        """get_progress() after get_results() reports from cached results, no live fetch."""
        mock_post_response = Mock()
        mock_post_response.status_code = 201
        mock_post_response.json.return_value = {"id": "search-123"}
        mock_post.return_value = mock_post_response

        mock_meta_response = Mock()
        mock_meta_response.status_code = 200
        mock_meta_response.json.return_value = {
            "id": "search-123",
            "isComplete": True,
            "responseCount": 2,
            "fileCount": 2,
        }
        mock_responses_response = Mock()
        mock_responses_response.status_code = 200
        mock_responses_response.json.return_value = [
            {"username": "peer1", "files": [{"filename": "a.mp3", "size": 1}], "hasFreeUploadSlot": True},
        ]
        mock_get.side_effect = [mock_meta_response, mock_responses_response]

        config = MockConfig()
        search = SlskdSearch(config)

        job = search.search("Test")
        search.get_results(job.search_id)

        mock_get.reset_mock()
        progress = search.get_progress(job.search_id)

        assert progress["is_complete"] is True
        assert progress["response_count"] == 1
        mock_get.assert_not_called()


class TestSlskdArtistFiltering:
    """Test artist filtering logic."""
    
    def test_extract_artist_words(self):
        """_extract_artist_words should extract meaningful words."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        words = search._extract_artist_words("Queen")
        assert "queen" in words
        
        words = search._extract_artist_words("The Beatles")
        assert "beatles" in words
        assert "the" not in words  # Stopword
        
        words = search._extract_artist_words("Taylor Swift")
        assert "taylor" in words
        assert "swift" in words
    
    def test_extract_artist_words_with_stopwords(self):
        """_extract_artist_words should truncate feat clause and drop stopwords."""
        config = MockConfig()
        search = SlskdSearch(config)

        words = search._extract_artist_words("Alesso feat. Katy Perry")
        assert words == ["alesso"]
        assert "katy" not in words  # Truncated with the feat clause (P6.5-6)
        assert "perry" not in words
        assert "feat" not in words
    
    def test_filter_by_artist(self):
        """_filter_by_artist should filter raw responses by artist words."""
        config = MockConfig()
        search = SlskdSearch(config)

        def _resp(username, filename):
            return {
                "username": username,
                "files": [{"filename": filename, "size": 5242880}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400,
            }

        results = [
            _resp("peer1", "Queen - Bohemian Rhapsody.mp3"),
            _resp("peer2", "Bohemian Rhapsody.mp3"),  # No artist
            _resp("peer3", "queen_bohemian_rhapsody.mp3"),
        ]

        filtered = search._filter_by_artist(results, "Queen")

        assert len(filtered) == 2
        assert all("queen" in r["files"][0]["filename"].lower() for r in filtered)

    def test_extract_artist_words_folds_accents(self):
        """2026-08-12 fix: an unfolded 'björk' only ever matched a filename
        that also kept the accent, and most Soulseek peers spell filenames
        in plain ASCII."""
        config = MockConfig()
        search = SlskdSearch(config)
        assert search._extract_artist_words("Björk") == ["bjork"]

    def test_filter_by_artist_matches_either_way_on_accents(self):
        """Folding both the artist words and the candidate filename means
        the match works regardless of which way a given peer spelled it."""
        config = MockConfig()
        search = SlskdSearch(config)

        def _resp(username, filename):
            return {
                "username": username,
                "files": [{"filename": filename, "size": 5242880}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 102400,
            }

        results = [
            _resp("peer1", "01 Bjork - Joga.flac"),  # peer stripped the accent
            _resp("peer2", "01 Björk - Jóga.flac"),  # peer kept it
            _resp("peer3", "01 Someone Else - Song.flac"),
        ]

        filtered = search._filter_by_artist(results, "Björk")

        assert len(filtered) == 2
        assert {r["username"] for r in filtered} == {"peer1", "peer2"}


class TestSlskdToSearchResult:
    """Test _to_search_result conversion."""
    
    def test_to_search_result_full(self):
        """_to_search_result should convert full response."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        response = {
            "username": "peer1",
            "files": [{"filename": "song.mp3", "size": 5242880, "bitRate": 320, "duration": 240}],
            "hasFreeUploadSlot": True,
            "uploadSpeed": 102400
        }
        
        result = search._to_search_result(response)
        
        assert result.username == "peer1"
        assert result.filename == "song.mp3"
        assert result.size == 5242880
        assert result.has_free_slot is True
        assert result.upload_speed == 102400
        assert result.bitrate == "320"
        assert result.duration == 240
    
    def test_to_search_result_minimal(self):
        """_to_search_result should handle minimal response."""
        config = MockConfig()
        search = SlskdSearch(config)
        
        response = {
            "username": "peer1",
            "files": [],
            "hasFreeUploadSlot": False
        }
        
        result = search._to_search_result(response)
        
        assert result.username == "peer1"
        assert result.filename == ""
        assert result.size == 0
        assert result.has_free_slot is False



class TestHeaderHydration:
    """After migration 005 the store holds search *headers* only.

    Hydration exists so a saved search still resolves after a restart; its
    results are then re-fetched from slskd, which is where they live.
    """

    @staticmethod
    def _make_config_with_db(tmpdir: str):
        from pathlib import Path

        from app.db.database import Database
        from app.db.search_store import SearchStore

        class _Paths:
            data_dir = str(Path(tmpdir) / "data")

        class _Slskd:
            url = "http://slskd:5030"
            api_key = "test-api-key"

        class _Search:
            response_threshold = 10
            min_wait_seconds = 0
            wait_seconds = 10
            poll_interval = 1

        class _Cfg:
            def __init__(self):
                self.slskd = _Slskd()
                self.search = _Search()
                self.paths = _Paths()

        config = _Cfg()
        database = Database(config)
        database.initialize_schema()
        return config, database, SearchStore(database)

    def test_headers_hydrate_and_results_refetch_after_restart(self, tmp_path):
        """The behavior that matters: a search survives a restart and its
        results come back — without musica having stored a single response."""
        config, _database, store = self._make_config_with_db(str(tmp_path))
        # The route owns header persistence for user-initiated searches.
        store.insert_search("search-hydrate-1", "bohemian rhapsody", None, "searching")

        search = SlskdSearch(config, store=store)

        mock_meta = Mock()
        mock_meta.status_code = 200
        mock_meta.json.return_value = {
            "id": "search-hydrate-1",
            "isComplete": True,
            "responseCount": 1,
            "fileCount": 1,
        }
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "username": "peer1",
                "files": [{"filename": "song.mp3", "size": 100}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 1,
            }
        ]
        with patch("app.services.search.requests.Session.get") as mock_get, patch(
            "app.services.search.requests.Session.post"
        ) as mock_post:
            mock_get.side_effect = [mock_meta, mock_resp]
            results = search.get_results("search-hydrate-1")
            # Re-read, never re-searched: no POST /api/v0/searches.
            mock_post.assert_not_called()

        assert len(results) == 1
        assert results[0].username == "peer1"

    def test_hydration_does_not_read_any_response_table(self, tmp_path):
        """Guard against the O(all-history) startup that 004 introduced:
        hydration must touch headers only."""
        config, _database, store = self._make_config_with_db(str(tmp_path))
        for i in range(3):
            store.insert_search(f"s{i}", f"q{i}", None, "completed")

        search = SlskdSearch(config, store=store)
        search._hydrate()

        assert set(search._searches) == {"s0", "s1", "s2"}
        assert search._responses == {}
        assert not hasattr(store, "all_responses")
        assert not hasattr(store, "replace_responses")

    def test_status_change_updates_the_header(self, tmp_path):
        config, _database, store = self._make_config_with_db(str(tmp_path))
        store.insert_search("search-status-1", "q", None, "searching")
        search = SlskdSearch(config, store=store)

        with patch("app.services.search.requests.Session.put") as mock_put:
            mock_put.return_value = Mock(status_code=200)
            assert search.cancel("search-status-1") is True

        assert store.get_search("search-status-1")["status"] == "cancelled"

    def test_rec_searches_never_get_a_header_row(self, tmp_path):
        """RecPuller's background searches must stay out of the user's own
        search history (the reason migration 003 dropped is_rec_search)."""
        config, _database, store = self._make_config_with_db(str(tmp_path))
        search = SlskdSearch(config, store=store)

        with patch("app.services.search.requests.Session.post") as mock_post:
            mock_post.return_value = Mock(
                status_code=201, json=Mock(return_value={"id": "rec-search-1"})
            )
            job = search.search("heroes alesso")

        assert job.search_id == "rec-search-1"
        assert store.get_search("rec-search-1") is None
        assert store.all_searches() == []


class TestDriveFailureIsNotCached:
    """A transient slskd failure must not be recorded as a completed,
    zero-result search — otherwise the cached [] is returned forever."""

    def _search(self):
        return SlskdSearch(MockConfig())

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_poll_failure_is_not_cached(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "drive-fail-1"})
        )
        job = search.search("Test")

        mock_get.side_effect = requests.exceptions.ConnectionError("slskd down")
        assert search.get_results(job.search_id) == []

        assert job.search_id not in search._responses
        assert search._searches[job.search_id].status == "searching"

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_search_recovers_on_the_next_call(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "drive-fail-2"})
        )
        job = search.search("Test")

        mock_get.side_effect = requests.exceptions.ConnectionError("slskd down")
        assert search.get_results(job.search_id) == []

        mock_meta = Mock()
        mock_meta.status_code = 200
        mock_meta.json.return_value = {
            "id": job.search_id,
            "isComplete": True,
            "responseCount": 1,
            "fileCount": 1,
        }
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "username": "peer1",
                "files": [{"filename": "song.mp3", "size": 100}],
                "hasFreeUploadSlot": True,
                "uploadSpeed": 1,
            }
        ]
        mock_get.side_effect = [mock_meta, mock_resp]

        results = search.get_results(job.search_id)
        assert len(results) == 1
        assert search._searches[job.search_id].status == "completed"


def _meta_response(search_id, response_count=1, file_count=1):
    return Mock(
        status_code=200,
        json=Mock(
            return_value={
                "id": search_id,
                "isComplete": True,
                "responseCount": response_count,
                "fileCount": file_count,
            }
        ),
    )


def _responses_response(files=None):
    files = files if files is not None else [{"filename": "song.mp3", "size": 100}]
    return Mock(
        status_code=200,
        json=Mock(
            return_value=[
                {
                    "username": "peer1",
                    "files": files,
                    "hasFreeUploadSlot": True,
                    "uploadSpeed": 1,
                }
            ]
        ),
    )


class TestSlskdRateLimiting:
    """The rate limiter gates every real slskd POST, and only that."""

    class _TightConfig(MockConfig):
        class SearchConfig(MockConfig.SearchConfig):
            rate_limit_max_searches = 2
            rate_limit_window_seconds = 60
            rate_limit_wait_timeout_seconds = 0.2
            query_cache_ttl_seconds = 0

        search = SearchConfig()

    @patch("app.services.search.requests.Session.post")
    def test_third_search_within_the_window_raises(self, mock_post):
        mock_post.return_value = Mock(
            status_code=201, json=Mock(side_effect=[{"id": "s1"}, {"id": "s2"}])
        )
        search = SlskdSearch(self._TightConfig())

        search.search("one")
        search.search("two")
        with pytest.raises(SearchRateLimitedError):
            search.search("three")

        assert mock_post.call_count == 2

    @patch("app.services.search.requests.Session.post")
    def test_slot_frees_up_after_the_window_elapses(self, mock_post):
        mock_post.return_value = Mock(
            status_code=201,
            json=Mock(side_effect=[{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]),
        )

        class _FastWindowConfig(MockConfig):
            class SearchConfig(MockConfig.SearchConfig):
                rate_limit_max_searches = 2
                rate_limit_window_seconds = 0.3
                rate_limit_wait_timeout_seconds = 2.0
                query_cache_ttl_seconds = 0

            search = SearchConfig()

        search = SlskdSearch(_FastWindowConfig())
        search.search("one")
        search.search("two")

        # Third call blocks until the window's oldest entry ages out, then
        # succeeds rather than raising — this is the whole point: callers
        # wait instead of superimposing spikes.
        job = search.search("three")
        assert job.search_id == "s3"
        assert mock_post.call_count == 3


class TestSlskdQueryCache:
    """Identical query text within the TTL must not touch slskd again."""

    class _CachingConfig(MockConfig):
        class SearchConfig(MockConfig.SearchConfig):
            query_cache_ttl_seconds = 600
            rate_limit_max_searches = 1000
            rate_limit_window_seconds = 1

        search = SearchConfig()

    def _search(self):
        return SlskdSearch(self._CachingConfig())

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_identical_query_after_a_real_drive_skips_slskd(
        self, mock_post, mock_get
    ):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [_meta_response("s1"), _responses_response()]

        job1 = search.search("Alright")
        results1 = search.get_results(job1.search_id)
        assert len(results1) == 1
        assert mock_post.call_count == 1

        # Second identical query: no new POST, no new GET either — the
        # whole point is that this never touches slskd.
        job2 = search.search("Alright")
        assert job2.search_id != job1.search_id
        results2 = search.get_results(job2.search_id)

        assert mock_post.call_count == 1
        assert mock_get.call_count == 2  # unchanged from the first drive
        assert [r.username for r in results2] == [r.username for r in results1]

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_cache_key_is_case_and_whitespace_insensitive(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [_meta_response("s1"), _responses_response()]

        search.get_results(search.search("Alright").search_id)
        job2 = search.search("  ALRIGHT  ")
        search.get_results(job2.search_id)

        assert mock_post.call_count == 1

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_cache_hit_still_applies_its_own_artist_filter(self, mock_post, mock_get):
        """Two callers can share one cached raw response set while each
        getting their own artist-filtered view of it."""
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [
            _meta_response("s1"),
            Mock(
                status_code=200,
                json=Mock(
                    return_value=[
                        {
                            "username": "peer1",
                            "files": [{"filename": "Kendrick Lamar - Alright.mp3"}],
                            "hasFreeUploadSlot": True,
                            "uploadSpeed": 1,
                        },
                        {
                            "username": "peer2",
                            "files": [{"filename": "Someone Else - Alright.mp3"}],
                            "hasFreeUploadSlot": True,
                            "uploadSpeed": 1,
                        },
                    ]
                ),
            ),
        ]

        no_filter = search.search("Alright")
        assert len(search.get_results(no_filter.search_id)) == 2

        filtered_job = search.search("Alright", artist="Kendrick Lamar")
        filtered = search.get_results(filtered_job.search_id)
        assert len(filtered) == 1
        assert filtered[0].username == "peer1"
        assert mock_post.call_count == 1

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_expired_entry_triggers_a_real_search_again(self, mock_post, mock_get):
        class _ShortTtlConfig(MockConfig):
            class SearchConfig(MockConfig.SearchConfig):
                query_cache_ttl_seconds = 0.05
                rate_limit_max_searches = 1000
                rate_limit_window_seconds = 1

            search = SearchConfig()

        search = SlskdSearch(_ShortTtlConfig())
        mock_post.return_value = Mock(
            status_code=201,
            json=Mock(side_effect=[{"id": "s1"}, {"id": "s2"}]),
        )
        mock_get.side_effect = [
            _meta_response("s1"),
            _responses_response(),
            _meta_response("s2"),
            _responses_response(),
        ]

        search.get_results(search.search("Alright").search_id)
        time.sleep(0.1)
        search.get_results(search.search("Alright").search_id)

        assert mock_post.call_count == 2

    @patch("app.services.search.requests.Session.post")
    def test_cache_hit_bypasses_the_rate_limiter(self, mock_post):
        """A cache hit costs nothing against the search budget — it never
        reaches slskd, so it must never consume a rate-limit slot either."""

        class _ZeroBudgetAfterOneConfig(MockConfig):
            class SearchConfig(MockConfig.SearchConfig):
                rate_limit_max_searches = 1
                rate_limit_window_seconds = 60
                rate_limit_wait_timeout_seconds = 0.1
                query_cache_ttl_seconds = 600

            search = SearchConfig()

        search = SlskdSearch(_ZeroBudgetAfterOneConfig())
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )

        # Seed the cache directly — this test is about acquire()/the limiter,
        # not about driving a full search first.
        search._query_cache["alright"] = (time.monotonic() + 600, [])

        search.search("Alright")  # cache hit — must not touch the limiter
        search.search("Alright")  # second cache hit — still must not raise
        assert mock_post.call_count == 0

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_cancel_on_a_cache_hit_never_calls_slskd(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [_meta_response("s1"), _responses_response()]

        search.get_results(search.search("Alright").search_id)
        cache_job = search.search("Alright")

        with patch("app.services.search.requests.Session.put") as mock_put:
            assert search.cancel(cache_job.search_id) is True
            mock_put.assert_not_called()
        assert search._searches[cache_job.search_id].status == "cancelled"

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_get_status_on_a_cache_hit_never_calls_slskd(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [_meta_response("s1"), _responses_response()]

        search.get_results(search.search("Alright").search_id)
        cache_job = search.search("Alright")

        mock_get.reset_mock()
        status = search.get_status(cache_job.search_id)
        assert status.search_id == cache_job.search_id
        mock_get.assert_not_called()

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_get_progress_on_a_cache_hit_never_calls_slskd(self, mock_post, mock_get):
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        mock_get.side_effect = [_meta_response("s1"), _responses_response()]

        search.get_results(search.search("Alright").search_id)
        cache_job = search.search("Alright")

        mock_get.reset_mock()
        progress = search.get_progress(cache_job.search_id)
        assert progress["is_complete"] is True
        assert progress["response_count"] == 1
        mock_get.assert_not_called()

    @patch("app.services.search.requests.Session.get")
    @patch("app.services.search.requests.Session.post")
    def test_a_real_drive_failure_does_not_seed_the_cache(self, mock_post, mock_get):
        """A failed drive must not poison later identical searches with an
        empty cached result set — matches the existing no-cache-on-failure
        rule for `_responses`, just at the query-cache layer too."""
        search = self._search()
        mock_post.return_value = Mock(
            status_code=201,
            json=Mock(side_effect=[{"id": "s1"}, {"id": "s2"}]),
        )
        mock_get.side_effect = requests.exceptions.ConnectionError("slskd down")

        assert search.get_results(search.search("Alright").search_id) == []

        mock_get.side_effect = [_meta_response("s2"), _responses_response()]
        assert len(search.get_results(search.search("Alright").search_id)) == 1
        assert mock_post.call_count == 2


class TestSlskdResponseLimitInPayload:
    def _search(self, response_limit=60):
        class _Config(MockConfig):
            class SearchConfig(MockConfig.SearchConfig):
                pass

            search = SearchConfig()

        cfg = _Config()
        cfg.search.response_limit = response_limit
        return SlskdSearch(cfg)

    @patch("app.services.search.requests.Session.post")
    def test_default_response_limit_is_sent(self, mock_post):
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        search = self._search()
        search.search("Test")
        assert mock_post.call_args[1]["json"]["responseLimit"] == 60

    @patch("app.services.search.requests.Session.post")
    def test_configured_response_limit_is_sent(self, mock_post):
        mock_post.return_value = Mock(
            status_code=201, json=Mock(return_value={"id": "s1"})
        )
        search = self._search(response_limit=75)
        search.search("Test")
        assert mock_post.call_args[1]["json"]["responseLimit"] == 75
