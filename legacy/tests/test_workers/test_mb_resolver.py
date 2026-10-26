"""Tests for MusicBrainz multi-track resolve behavior."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.database import Database
from app.services.interfaces.download import QueueResult
from app.services.interfaces.search import SearchJob, SearchResult
from app.workers.mb_resolver import _resolve_one


class FakeSearchService:
    def search(self, query):
        return SearchJob(
            search_id=f"search-{query}",
            query=query,
            artist=None,
            created_at=datetime.now(timezone.utc),
            status="completed",
        )

    def get_results(self, search_id):
        return [
            SearchResult("peer-one", "Artist - Track.mp3", 1000, True, 10, None, None),
            SearchResult("peer-two", "Artist - Track.flac", 2000, True, 20, None, None),
        ]


class FakeDownloadService:
    def __init__(self):
        self.queue_calls = []

    def queue(self, username, files, search_id=None):
        self.queue_calls.append((username, files, search_id))
        return QueueResult(1, [], search_id)


class FailingDownloadService(FakeDownloadService):
    def queue(self, username, files, search_id=None):
        self.queue_calls.append((username, files, search_id))
        raise RuntimeError("peer endpoint unavailable")


class ManyCandidatesSearchService(FakeSearchService):
    def get_results(self, search_id):
        return [
            SearchResult(
                f"peer-{number}",
                "Artist - Track.mp3",
                1000,
                True,
                10,
                None,
                None,
            )
            for number in range(10)
        ]


class FakeEventHub:
    def __init__(self):
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))


def _config():
    return SimpleNamespace(
        search=SimpleNamespace(pass_ratio_threshold=0.5, artist_match_min_words=1),
        download=SimpleNamespace(
            peer_ban_days=2, bad_peer_threshold=1, max_retries_per_track=3
        ),
    )


def test_album_tracks_spread_across_unused_peers_before_reusing_one():
    search = FakeSearchService()
    download = FakeDownloadService()
    events = FakeEventHub()
    used_peers = set()

    for number in (1, 2):
        queued = _resolve_one(
            job_id="album-job",
            recording=SimpleNamespace(
                title=f"Track {number}", artist="Artist", mbid=f"mbid-{number}"
            ),
            search_service=search,
            download_service=download,
            config=_config(),
            db=None,
            event_hub=events,
            used_peers=used_peers,
        )
        assert queued is True

    assert [call[0] for call in download.queue_calls] == ["peer-one", "peer-two"]


def test_album_track_queue_attempts_use_the_configured_retry_budget():
    """max_retries_per_track = N means N retries after the original attempt:
    N+1 peers in all."""
    config = _config()
    download = FailingDownloadService()

    queued = _resolve_one(
        job_id="album-job",
        recording=SimpleNamespace(title="Track", artist="Artist", mbid="mbid"),
        search_service=ManyCandidatesSearchService(),
        download_service=download,
        config=config,
        db=None,
        event_hub=FakeEventHub(),
        used_peers=set(),
    )

    assert queued is False
    assert [call[0] for call in download.queue_calls] == [
        "peer-0",
        "peer-1",
        "peer-2",
        "peer-3",
    ]


class FailFirstDownloadService(FakeDownloadService):
    """Refuses the first `failures` queue calls, accepts the rest."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def queue(self, username, files, search_id=None):
        self.queue_calls.append((username, files, search_id))
        if len(self.queue_calls) <= self.failures:
            raise RuntimeError("peer refused")
        return QueueResult(1, [], search_id)


@pytest.fixture
def db(tmp_path):
    class MockPaths:
        pass

    class MockConfig:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmp_path) / "data")
    cfg = MockConfig()
    cfg.paths = paths
    database = Database(cfg)
    database.initialize_schema()
    yield database
    database.close()


def test_one_request_and_one_download_row_across_peer_retries(db):
    """A track that needs several peers keeps the one request and the one
    download row it started with; each further peer is a retry on that row,
    recorded as its own attempt."""
    download = FailFirstDownloadService(failures=2)

    queued = _resolve_one(
        job_id="album-job",
        recording=SimpleNamespace(title="Track", artist="Artist", mbid="mbid-1"),
        search_service=ManyCandidatesSearchService(),
        download_service=download,
        config=_config(),
        db=db,
        event_hub=FakeEventHub(),
        used_peers=set(),
    )

    assert queued is True
    assert db.fetch_one("SELECT COUNT(*) AS n FROM requests")["n"] == 1
    rows = db.fetch_all("SELECT id, username, state FROM downloads")
    assert len(rows) == 1
    assert rows[0]["username"] == "peer-2"
    assert rows[0]["state"] == "queued"
    attempts = db.fetch_all(
        "SELECT username, success FROM download_attempts WHERE download_id = ? ORDER BY id",
        (rows[0]["id"],),
    )
    assert [(a["username"], a["success"]) for a in attempts] == [
        ("peer-0", 0),
        ("peer-1", 0),
        ("peer-2", 1),
    ]


def test_exhausted_track_leaves_one_failed_row_with_every_peer_recorded(db):
    download = FailingDownloadService()

    queued = _resolve_one(
        job_id="album-job",
        recording=SimpleNamespace(title="Track", artist="Artist", mbid="mbid-2"),
        search_service=ManyCandidatesSearchService(),
        download_service=download,
        config=_config(),
        db=db,
        event_hub=FakeEventHub(),
        used_peers=set(),
    )

    assert queued is False
    rows = db.fetch_all("SELECT id, state FROM downloads")
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM requests")["n"] == 1
    attempts = db.fetch_one(
        "SELECT COUNT(*) AS n FROM download_attempts WHERE download_id = ?",
        (rows[0]["id"],),
    )
    assert attempts["n"] == 4
