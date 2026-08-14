"""Tests for MusicBrainz multi-track resolve behavior."""

from datetime import datetime, timezone
from types import SimpleNamespace

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
        download=SimpleNamespace(peer_ban_days=2, bad_peer_threshold=1),
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
    config = _config()
    config.download.max_retries_per_track = 3
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
    ]
