"""
Integration tests for Search API routes (P4-1).

Uses FastAPI TestClient with a fake SearchService injected via create_app().
Exercises the 4 endpoints plus the global error format:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.exceptions import (
    SearchNotFoundError,
    SearchRateLimitedError,
    SlskdConnectionError,
)
from app.main import create_app
from app.services.interfaces.search import SearchJob, SearchResult, SearchService


class FakeSearchService(SearchService):
    """In-memory SearchService that raises real exceptions."""

    def __init__(self):
        self.searches = {}
        self.next_id = 1
        self.fail_search = False  # Simulate slskd connection failure
        self.rate_limited = False  # Simulate the rate limiter's wait timing out
        self.progress_overrides = {}

    def search(self, query: str, artist: str | None = None) -> SearchJob:
        if self.fail_search:
            raise SlskdConnectionError("http://slskd:5030", "Connection refused")
        if self.rate_limited:
            raise SearchRateLimitedError(max_searches=4, window_seconds=60)
        search_id = f"search-{self.next_id}"
        self.next_id += 1
        job = SearchJob(
            search_id=search_id,
            query=query,
            artist=artist,
            created_at=datetime.now(timezone.utc),
            status="searching",
        )
        self.searches[search_id] = job
        return job

    def get_results(self, search_id: str) -> list[SearchResult]:
        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        return [
            SearchResult(
                username="peer1",
                filename="song.mp3",
                size=5242880,
                has_free_slot=True,
                upload_speed=102400,
                bitrate="320kbps",
                duration=240,
            )
        ]

    def cancel(self, search_id: str) -> bool:
        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        self.searches[search_id].status = "cancelled"
        return True

    def get_status(self, search_id: str) -> SearchJob:
        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        return self.searches[search_id]

    def list_searches(self) -> list[SearchJob]:
        return sorted(
            self.searches.values(),
            key=lambda job: job.created_at,
            reverse=True,
        )

    def get_progress(self, search_id: str) -> dict:
        if search_id not in self.searches:
            raise SearchNotFoundError(search_id)
        if search_id in self.progress_overrides:
            return self.progress_overrides[search_id]
        return {
            "response_count": 3,
            "file_count": 7,
            "is_complete": False,
            "elapsed_seconds": 2.5,
            "threshold": 10,
            "max_wait_seconds": 10,
            "response_cap": 250,
        }


@pytest.fixture
def client():
    service = FakeSearchService()
    app = create_app(search_service=service)
    return TestClient(app)


# ============================================================================
# POST /api/search
# ============================================================================


class TestCreateSearch:
    def test_search_success(self, client):
        """Valid request returns 201 with search job."""
        resp = client.post(
            "/api/search", json={"query": "Bohemian Rhapsody", "artist": "Queen"}
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["search_id"] == "search-1"
        assert body["query"] == "Bohemian Rhapsody"
        assert body["artist"] == "Queen"
        assert body["status"] == "searching"
        assert body["created_at"]  # ISO timestamp present

    def test_search_without_artist(self, client):
        """Artist is optional."""
        resp = client.post("/api/search", json={"query": "Bohemian Rhapsody"})

        assert resp.status_code == 201
        assert resp.json()["artist"] is None

    def test_search_missing_query(self, client):
        """Missing query returns 400 VALIDATION_ERROR."""
        resp = client.post("/api/search", json={})

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["message"]
        assert "errors" in body["error"]["details"]

    def test_search_blank_query(self, client):
        """Whitespace-only query and no artist returns 400."""
        resp = client.post("/api/search", json={"query": "   "})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_artist_only_search_uses_artist_as_query(self, client):
        """Empty query + artist: artist becomes the slskd query, no post-filter."""
        resp = client.post("/api/search", json={"query": "", "artist": "Queen"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["query"] == "Queen"
        assert body["artist"] is None

    def test_artist_only_search_missing_query_field(self, client):
        """Same as above but query field omitted entirely."""
        resp = client.post("/api/search", json={"artist": "Daft Punk"})

        assert resp.status_code == 201
        body = resp.json()
        assert body["query"] == "Daft Punk"
        assert body["artist"] is None

    def test_query_and_artist_both_given_unchanged(self, client):
        """query+artist: normal behavior, artist stays as post-filter."""
        resp = client.post(
            "/api/search", json={"query": "Get Lucky", "artist": "Daft Punk"}
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["query"] == "Get Lucky"
        assert body["artist"] == "Daft Punk"

    def test_both_query_and_artist_blank_400(self, client):
        """Whitespace-only query and blank artist returns 400."""
        resp = client.post("/api/search", json={"query": "  ", "artist": "  "})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_search_connection_error(self, client):
        """slskd connection failure returns 503 SLSKD_CONNECTION_FAILED."""
        service = client.app.state.services["search"]
        service.fail_search = True

        resp = client.post("/api/search", json={"query": "Bohemian Rhapsody"})

        assert resp.status_code == 503
        body = resp.json()
        assert body["error"]["code"] == "SLSKD_CONNECTION_FAILED"
        assert body["error"]["details"]["url"] == "http://slskd:5030"

    def test_search_rate_limited(self, client):
        """No free slot in time returns 429 SEARCH_RATE_LIMITED."""
        service = client.app.state.services["search"]
        service.rate_limited = True

        resp = client.post("/api/search", json={"query": "Bohemian Rhapsody"})

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == "SEARCH_RATE_LIMITED"
        assert body["error"]["details"]["max_searches"] == 4


# ============================================================================
# GET /api/searches
# ============================================================================


class TestListSearches:
    def test_list_empty(self, client):
        """No searches returns empty list."""
        resp = client.get("/api/searches")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_all_newest_first(self, client):
        """Returns all searches, newest first."""
        client.post("/api/search", json={"query": "First"})
        client.post("/api/search", json={"query": "Second"})

        resp = client.get("/api/searches")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0]["query"] == "Second"
        assert body[1]["query"] == "First"
        assert body[0]["search_id"] != body[1]["search_id"]


# ============================================================================
# GET /api/searches/{search_id}
# ============================================================================


class TestGetSearch:
    def test_get_search_details_and_results(self, client):
        """Returns search details plus results."""
        created = client.post(
            "/api/search", json={"query": "Bohemian Rhapsody", "artist": "Queen"}
        ).json()

        resp = client.get(f"/api/searches/{created['search_id']}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["search"]["search_id"] == created["search_id"]
        assert body["search"]["query"] == "Bohemian Rhapsody"
        assert len(body["results"]) == 1
        result = body["results"][0]
        assert result["username"] == "peer1"
        assert result["filename"] == "song.mp3"
        assert result["size"] == 5242880
        assert result["has_free_slot"] is True
        assert result["upload_speed"] == 102400
        assert result["bitrate"] == "320kbps"
        assert result["duration"] == 240

    def test_get_search_not_found(self, client):
        """Unknown search_id returns 404 SEARCH_NOT_FOUND."""
        resp = client.get("/api/searches/nope")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "SEARCH_NOT_FOUND"
        assert body["error"]["details"] == {"search_id": "nope"}


# ============================================================================
# GET /api/searches/{search_id}/progress
# ============================================================================


class TestGetSearchProgress:
    def test_progress_success(self, client):
        """Returns live progress counts for an in-progress search."""
        created = client.post("/api/search", json={"query": "Bohemian Rhapsody"}).json()

        resp = client.get(f"/api/searches/{created['search_id']}/progress")

        assert resp.status_code == 200
        body = resp.json()
        assert body["response_count"] == 3
        assert body["file_count"] == 7
        assert body["is_complete"] is False
        assert body["threshold"] == 10
        assert body["max_wait_seconds"] == 10
        assert body["response_cap"] == 250

    def test_progress_not_found(self, client):
        """Unknown search_id returns 404 SEARCH_NOT_FOUND."""
        resp = client.get("/api/searches/nope/progress")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SEARCH_NOT_FOUND"

    def test_progress_does_not_drive_completion(self, client):
        """Progress peek must not fetch/complete results as a side effect."""
        created = client.post("/api/search", json={"query": "Test"}).json()
        service = client.app.state.services["search"]
        service.progress_overrides[created["search_id"]] = {
            "response_count": 1,
            "file_count": 1,
            "is_complete": False,
            "elapsed_seconds": 0.5,
            "threshold": 10,
            "max_wait_seconds": 10,
        }

        client.get(f"/api/searches/{created['search_id']}/progress")

        # Status should remain "searching" — progress alone shouldn't
        # advance it to "completed" the way GET /searches/{id} does.
        detail_status = service.get_status(created["search_id"])
        assert detail_status.status == "searching"


# ============================================================================
# POST /api/searches/{search_id}/cancel
# ============================================================================


class TestCancelSearch:
    def test_cancel_success(self, client):
        """Cancelling an existing search returns cancelled=true."""
        created = client.post("/api/search", json={"query": "Bohemian Rhapsody"}).json()

        resp = client.post(f"/api/searches/{created['search_id']}/cancel")

        assert resp.status_code == 200
        assert resp.json() == {"search_id": created["search_id"], "cancelled": True}

        # Status reflects cancellation
        detail = client.get(f"/api/searches/{created['search_id']}").json()
        assert detail["search"]["status"] == "cancelled"

    def test_cancel_not_found(self, client):
        """Cancelling an unknown search returns 404."""
        resp = client.post("/api/searches/nope/cancel")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SEARCH_NOT_FOUND"


# ============================================================================
# Global error handling
# ============================================================================


class TestErrorFormat:
    def test_unknown_route_returns_404_format(self, client):
        """Unmatched route returns 404 with error format."""
        resp = client.get("/api/nonexistent")

        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "HTTP_404"
        assert "error" in body

    def test_default_app_uses_real_service(self):
        """create_app() with no args works with defaults (no config file needed)."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/api/searches")

        assert resp.status_code == 200
        assert resp.json() == []


class TestStaticServing:
    def test_root_serves_placeholder_ui(self):
        """GET / serves the placeholder UI when app/static/index.html exists."""
        app = create_app()
        client = TestClient(app)

        resp = client.get("/")

        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Post-Vinyl" in resp.text
