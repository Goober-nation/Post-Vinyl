"""
Integration tests for MusicBrainz API routes (Phase 6.8).

Uses FastAPI TestClient with fake MusicBrainzService / SearchService /
DownloadService. The MusicBrainz service has no `create_app(...)` parameter,
so it is injected onto `app.state.services["musicbrainz"]` after `create_app()`
(same pattern as `app.state.rec_puller` in test_recs_routes.py).

The two POST download endpoints are asserted only for their 202 + job_id —
the spawned resolve thread is a no-op because the fakes return no recordings.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.interfaces.download import QueueResult
from app.services.interfaces.musicbrainz import (
    MBArtist,
    MBRecording,
    MBRelease,
    MBReleaseGroup,
)

VALID_MBID = "b0ba2b46-9cd7-4c8b-a8a6-6c1a5c1d5b1a"


class FakeMusicBrainzService:
    """In-memory MusicBrainz service returning scripted dataclasses."""

    def __init__(self):
        self.recordings: list[MBRecording] = []
        self.release_groups: list[MBReleaseGroup] = []
        self.artists: list[MBArtist] = []
        self.album_tracks: list[MBRecording] = []
        # Record the official_only value each endpoint forwarded, so tests can
        # assert the config knob reaches the service.
        self.recording_official_only: bool | None = None
        self.release_group_official_only: bool | None = None
        self.browse_official_only: bool | None = None
        self.recording_calls: list[tuple] = []
        self.release_group_calls: list[tuple] = []
        self.artist_calls: list[tuple] = []

    def search_recording(self, title, artist=None, limit=10, official_only=False):
        self.recording_official_only = official_only
        self.recording_calls.append((title, artist, limit, official_only))
        return self.recordings

    def search_release_group(self, title, artist=None, limit=10, official_only=False):
        self.release_group_official_only = official_only
        self.release_group_calls.append((title, artist, limit, official_only))
        return self.release_groups

    def search_artist(self, name, limit=10):
        self.artist_calls.append((name, limit))
        return self.artists

    def browse_artist_release_groups(self, artist_mbid, limit=100, official_only=False):
        self.browse_official_only = official_only
        return self.release_groups

    def lookup_release_group_tracks(self, release_group_mbid):
        return self.album_tracks

    def lookup_recording(self, mbid):
        return None


class FakeSearchService:
    def search(self, query, artist=None):
        raise NotImplementedError

    def get_results(self, search_id):
        return []

    def cancel(self, search_id):
        return True

    def get_status(self, search_id):
        raise NotImplementedError

    def list_searches(self):
        return []

    def get_progress(self, search_id):
        return {}


class FakeDownloadService:
    def queue(self, username, files, search_id=None, destination=None):
        return QueueResult(enqueued_count=0, failures=[], search_id=search_id)

    def get_status(self):
        return []

    def retry(self, transfer_id):
        raise NotImplementedError

    def cancel(self, transfer_id):
        return True

    def delete_transfer(self, transfer_id):
        return True

    def get_transfer(self, transfer_id):
        raise NotImplementedError


@pytest.fixture
def client():
    musicbrainz = FakeMusicBrainzService()
    app = create_app(
        search_service=FakeSearchService(),
        download_service=FakeDownloadService(),
    )
    app.state.services["musicbrainz"] = musicbrainz
    return TestClient(app), musicbrainz


# ============================================================================
# GET /api/musicbrainz/search/recordings
# ============================================================================


class TestSearchRecordings:
    def test_shape(self, client):
        client, mb = client
        mb.recordings = [
            MBRecording(
                mbid="rec-1",
                title="Jóga",
                artist_credit="Björk",
                artist="Björk",
                artist_mbid="art-1",
                length_ms=234000,
                score=100,
                releases=[
                    MBRelease(
                        mbid="rel-1",
                        title="Homogenic",
                        primary_type="Album",
                        date="1997-09-22",
                        release_group_mbid="rg-1",
                    )
                ],
            )
        ]

        resp = client.get(
            "/api/musicbrainz/search/recordings",
            params={"title": "Joga", "artist": "Björk"},
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result == {
            "mbid": "rec-1",
            "title": "Jóga",
            "artist": "Björk",
            "artist_credit": "Björk",
            "album": "Homogenic",
            "year": 1997,
            "length_ms": 234000,
            "score": 100,
            "cover_mbid": "rg-1",
            "release_count": 1,
        }

    def test_one_field_query_tries_title_artist_splits(self, client):
        client, mb = client
        mb.recordings = [
            MBRecording(
                mbid="rec-damn",
                title="DAMN.",
                artist_credit="Kendrick Lamar",
                artist="Kendrick Lamar",
                score=100,
            )
        ]

        resp = client.get(
            "/api/musicbrainz/search/recordings",
            params={"title": "damn kendrick lamar"},
        )

        assert resp.status_code == 200
        assert resp.json()["results"][0]["title"] == "DAMN."
        assert ("damn", "kendrick lamar", 20, True) in mb.recording_calls


# ============================================================================
# GET /api/musicbrainz/search/albums
# ============================================================================


class TestSearchAlbums:
    def test_shape(self, client):
        client, mb = client
        mb.release_groups = [
            MBReleaseGroup(
                mbid="rg-1",
                title="Homogenic",
                artist="Björk",
                artist_mbid="art-1",
                primary_type="Album",
                year=1997,
            )
        ]

        resp = client.get(
            "/api/musicbrainz/search/albums", params={"title": "Homogenic"}
        )

        assert resp.status_code == 200
        assert resp.json()["results"] == [
            {
                "mbid": "rg-1",
                "title": "Homogenic",
                "artist": "Björk",
                "primary_type": "Album",
                "year": 1997,
                "release_count": 0,
            }
        ]

    def test_one_field_query_tries_title_artist_splits(self, client):
        client, mb = client
        mb.release_groups = [
            MBReleaseGroup(
                mbid="rg-damn",
                title="DAMN.",
                artist="Kendrick Lamar",
                primary_type="Album",
                year=2017,
                release_count=16,
                score=100,
            )
        ]

        resp = client.get(
            "/api/musicbrainz/search/albums",
            params={"title": "damn kendrick lamar", "sort": "prominence"},
        )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["title"] == "DAMN."
        assert result["release_count"] == 16
        assert ("damn", "kendrick lamar", 20, True) in mb.release_group_calls


class TestUnifiedSearch:
    def test_returns_artist_first_then_matching_album(self, client):
        client, mb = client
        mb.artists = [
            MBArtist(
                mbid="artist-kendrick",
                name="Kendrick Lamar",
                sort_name="Lamar, Kendrick",
                score=100,
            )
        ]
        mb.release_groups = [
            MBReleaseGroup(
                mbid="rg-damn",
                title="DAMN.",
                artist="Kendrick Lamar",
                primary_type="Album",
                year=2017,
                release_count=16,
                score=100,
            )
        ]

        resp = client.get(
            "/api/musicbrainz/search", params={"query": "kendrick lamar damn"}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["artist"]["name"] == "Kendrick Lamar"
        assert body["albums"][0]["title"] == "DAMN."
        assert ("Kendrick Lamar", 5) in mb.artist_calls

    def test_artist_only_query_still_returns_the_artist(self, client):
        client, mb = client
        mb.artists = [
            MBArtist(
                mbid="artist-kendrick",
                name="Kendrick Lamar",
                sort_name="Lamar, Kendrick",
                score=100,
            )
        ]

        resp = client.get("/api/musicbrainz/search", params={"query": "Kendrick Lamar"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["artist"]["name"] == "Kendrick Lamar"
        assert body["albums"] == []
        assert body["recordings"] == []


# ============================================================================
# GET /api/musicbrainz/search/artists
# ============================================================================


class TestSearchArtists:
    def test_shape(self, client):
        client, mb = client
        mb.artists = [
            MBArtist(
                mbid="art-1",
                name="Björk",
                sort_name="Björk",
                disambiguation=None,
                score=100,
            )
        ]

        resp = client.get("/api/musicbrainz/search/artists", params={"name": "Björk"})

        assert resp.status_code == 200
        assert resp.json()["results"] == [
            {
                "mbid": "art-1",
                "name": "Björk",
                "sort_name": "Björk",
                "disambiguation": None,
                "score": 100,
            }
        ]


# ============================================================================
# GET /api/musicbrainz/artists/{mbid}/albums
# ============================================================================


class TestArtistAlbums:
    def test_shape(self, client):
        client, mb = client
        mb.release_groups = [
            MBReleaseGroup(
                mbid="rg-1",
                title="Vespertine",
                artist="Björk",
                artist_mbid="art-1",
                primary_type="Album",
                year=2001,
            )
        ]

        resp = client.get(f"/api/musicbrainz/artists/{VALID_MBID}/albums")

        assert resp.status_code == 200
        assert resp.json()["results"] == [
            {
                "mbid": "rg-1",
                "title": "Vespertine",
                "artist": "Björk",
                "primary_type": "Album",
                "year": 2001,
                "release_count": 0,
            }
        ]


# ============================================================================
# GET /api/musicbrainz/albums/{mbid}/tracks
# ============================================================================


class TestAlbumTracks:
    def test_shape(self, client):
        client, mb = client
        mb.album_tracks = [
            MBRecording(
                mbid="rec-1",
                title="Hidden Place",
                artist_credit="Björk",
                artist="Björk",
                length_ms=312000,
            )
        ]

        resp = client.get(f"/api/musicbrainz/albums/{VALID_MBID}/tracks")

        assert resp.status_code == 200
        assert resp.json()["results"] == [
            {
                "mbid": "rec-1",
                "title": "Hidden Place",
                "artist": "Björk",
                "length_ms": 312000,
            }
        ]


# ============================================================================
# POST /api/musicbrainz/recordings/{mbid}/download
# ============================================================================


class TestDownloadRecording:
    def test_starts_job_202(self, client):
        client, _mb = client

        resp = client.post(f"/api/musicbrainz/recordings/{VALID_MBID}/download")

        assert resp.status_code == 202
        body = resp.json()
        assert body["started"] is True
        assert body["job_id"]  # non-empty uuid hex

    def test_invalid_mbid_404(self, client):
        client, _mb = client

        resp = client.post("/api/musicbrainz/recordings/not-a-uuid/download")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MUSICBRAINZ_NOT_FOUND"


# ============================================================================
# POST /api/musicbrainz/albums/{mbid}/download
# ============================================================================


class TestDownloadAlbum:
    def test_starts_job_202(self, client):
        client, _mb = client

        resp = client.post(f"/api/musicbrainz/albums/{VALID_MBID}/download")

        assert resp.status_code == 202
        body = resp.json()
        assert body["started"] is True
        assert body["job_id"]

    def test_invalid_mbid_404(self, client):
        client, _mb = client

        resp = client.post("/api/musicbrainz/albums/not-a-uuid/download")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MUSICBRAINZ_NOT_FOUND"


class TestOfficialOnlyForwarding:
    """The route forwards `musicbrainz.search_official_only` to the service."""

    def test_search_recordings_forwards_official_only(self, client):
        client, mb = client
        resp = client.get(
            "/api/musicbrainz/search/recordings", params={"title": "Joga"}
        )
        assert resp.status_code == 200
        assert mb.recording_official_only is True  # config default is True

    def test_search_albums_forwards_official_only(self, client):
        client, mb = client
        resp = client.get("/api/musicbrainz/search/albums", params={"title": "X"})
        assert resp.status_code == 200
        assert mb.release_group_official_only is True

    def test_artist_albums_forwards_official_only(self, client):
        client, mb = client
        resp = client.get(f"/api/musicbrainz/artists/{VALID_MBID}/albums")
        assert resp.status_code == 200
        assert mb.browse_official_only is True
