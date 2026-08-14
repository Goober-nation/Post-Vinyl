"""
Integration tests for Library API routes (P4-3).

Uses FastAPI TestClient with a FakeLibraryService injected via create_app().
Exercises the 3 endpoints plus the global error format:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

import pytest
from fastapi.testclient import TestClient

from app.exceptions import NavidromeConnectionError, PlaylistNotFoundError
from app.main import create_app
from app.services.library import LibraryService, PlaylistDetail, PlaylistInfo, Song


class FakeLibraryService(LibraryService):
    """In-memory LibraryService that raises real exceptions."""

    def __init__(self):
        self.playlists: dict[str, PlaylistInfo] = {}
        self.details: dict[str, PlaylistDetail] = {}
        self.fail_connection = False

    def trigger_scan(self) -> bool:
        if self.fail_connection:
            raise NavidromeConnectionError("http://navidrome:8090", "Connection refused")
        return True

    def list_playlists(self) -> list[PlaylistInfo]:
        if self.fail_connection:
            raise NavidromeConnectionError("http://navidrome:8090", "Connection refused")
        return list(self.playlists.values())

    def get_playlist_detail(self, playlist_id: str) -> PlaylistDetail:
        if self.fail_connection:
            raise NavidromeConnectionError("http://navidrome:8090", "Connection refused")
        if playlist_id not in self.details:
            raise PlaylistNotFoundError(playlist_id)
        return self.details[playlist_id]


def _make_song(song_id: str, title: str, artist: str = "Test Artist") -> Song:
    """Build a Song object for test fixtures."""
    return Song(
        song_id=song_id,
        title=title,
        artist=artist,
        album="Test Album",
        path=f"/music/{artist}/{title}.mp3",
        duration=200,
        size=4800000,
        bitrate=320,
        track_number=1,
        year=2024,
        genre="Rock",
        rating=4,
        starred=False,
        mbid="abc-123",
    )


def _make_playlist_detail(
    playlist_id: str, name: str, song_ids: list[str] | None = None
) -> PlaylistDetail:
    """Build a PlaylistDetail with one or two songs for test fixtures."""
    if song_ids is None:
        song_ids = [f"{playlist_id}-song-1"]
    if len(song_ids) == 1:
        songs = [_make_song(song_ids[0], "Song One")]
    else:
        songs = [_make_song(song_ids[0], "Song One"), _make_song(song_ids[1], "Song Two")]
    return PlaylistDetail(playlist_id=playlist_id, name=name, songs=songs)


@pytest.fixture
def client():
    service = FakeLibraryService()
    app = create_app(library_service=service)
    return TestClient(app)


@pytest.fixture
def service(client):
    return client.app.state.services["library"]


# ============================================================================
# POST /api/library/scan
# ============================================================================


class TestScan:
    def test_scan_success(self, client):
        """Scan returns 200 with scan_triggered=True."""
        resp = client.post("/api/library/scan")

        assert resp.status_code == 200
        assert resp.json() == {"scan_triggered": True}

    def test_scan_connection_error(self, client, service):
        """Connection failure returns 503 NAVIDROME_CONNECTION_FAILED."""
        service.fail_connection = True

        resp = client.post("/api/library/scan")

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "NAVIDROME_CONNECTION_FAILED"


# ============================================================================
# GET /api/playlists
# ============================================================================


class TestListPlaylists:
    def test_empty(self, client):
        """No playlists returns empty list."""
        resp = client.get("/api/playlists")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_with_playlists(self, client, service):
        """Returns playlist summaries with all fields."""
        service.playlists["p1"] = PlaylistInfo(
            playlist_id="p1",
            name="My Playlist",
            song_count=42,
            duration=3600,
            public=True,
            owner="user1",
            comment="A test playlist",
            created="2024-01-01T00:00:00Z",
            changed="2024-06-15T12:30:00Z",
        )

        resp = client.get("/api/playlists")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        p = body[0]
        assert p["playlist_id"] == "p1"
        assert p["name"] == "My Playlist"
        assert p["song_count"] == 42
        assert p["duration"] == 3600
        assert p["public"] is True
        assert p["owner"] == "user1"
        assert p["comment"] == "A test playlist"
        assert p["created"] == "2024-01-01T00:00:00Z"
        assert p["changed"] == "2024-06-15T12:30:00Z"

    def test_connection_error(self, client, service):
        """Connection failure returns 503."""
        service.fail_connection = True

        resp = client.get("/api/playlists")

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "NAVIDROME_CONNECTION_FAILED"


# ============================================================================
# POST /api/playlists/{playlist_id}/sync
# ============================================================================


class TestSync:
    def test_sync_success(self, client, service):
        """Sync returns playlist detail with songs."""
        service.details["p1"] = _make_playlist_detail("p1", "My Playlist")

        resp = client.post("/api/playlists/p1/sync")

        assert resp.status_code == 200
        body = resp.json()
        assert body["playlist_id"] == "p1"
        assert body["name"] == "My Playlist"
        assert body["song_count"] == 1
        assert len(body["songs"]) == 1
        song = body["songs"][0]
        assert song["song_id"] == "p1-song-1"
        assert song["title"] == "Song One"
        assert song["artist"] == "Test Artist"
        assert song["album"] == "Test Album"
        assert song["duration"] == 200
        assert song["size"] == 4800000
        assert song["bitrate"] == 320
        assert song["track_number"] == 1
        assert song["year"] == 2024
        assert song["genre"] == "Rock"
        assert song["rating"] == 4
        assert song["starred"] is False
        assert song["mbid"] == "abc-123"

    def test_sync_not_found(self, client):
        """Unknown playlist returns 404 PLAYLIST_NOT_FOUND."""
        resp = client.post("/api/playlists/nonexistent/sync")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLAYLIST_NOT_FOUND"

    def test_sync_connection_error(self, client, service):
        """Connection failure returns 503."""
        service.details["p1"] = _make_playlist_detail("p1", "My Playlist")
        service.fail_connection = True

        resp = client.post("/api/playlists/p1/sync")

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "NAVIDROME_CONNECTION_FAILED"
