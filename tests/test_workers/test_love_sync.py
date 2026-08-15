"""
Tests for the LoveSync worker (P6.7-5).
"""

import pytest

from app.db.database import Database
from app.db.sync_store import LOVE, SyncStore
from app.exceptions import ListenBrainzDisabledError
from app.services.library import Song
from app.workers.love_sync import LoveSync


def _make_config(tmpdir):
    from pathlib import Path

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmpdir) / "data")

    class MockSync:
        interval_hours = 12
        love_enabled = True
        hate_enabled = True
        star_rating_enabled = True
        trash_deletion_enabled = True

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.sync = MockSync()
    return cfg


def _song(song_id, title, mbid, rating=0, artist="Artist"):
    return Song(
        song_id=song_id,
        title=title,
        artist=artist,
        album="Album",
        path="",
        duration=200,
        size=1000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=rating,
        starred=True,
        mbid=mbid,
    )


class FakeLibraryService:
    def __init__(self, starred=None):
        self.starred = starred or []
        self.ratings: dict[str, int] = {}

    def get_starred(self):
        return list(self.starred)

    def set_rating(self, song_id, rating):
        self.ratings[song_id] = rating
        return True


class FakeFeedbackService:
    def __init__(self, disabled=False, results=None):
        self.disabled = disabled
        self.results = results or {}
        self.sent: list[tuple[str, int]] = []

    def send_feedback(self, mbid, score):
        if self.disabled:
            raise ListenBrainzDisabledError()
        self.sent.append((mbid, score))
        return self.results.get(mbid, True)


@pytest.fixture
def db(tmp_path):
    config = _make_config(str(tmp_path))
    database = Database(config)
    database.initialize_schema()
    yield database
    database.close()


def _make_worker(config, db, library, feedback):
    return LoveSync(config, library, feedback, db)


class TestRating:
    def test_unrated_favorite_gets_five_stars(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1", rating=0)])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.sync_once()

        assert library.ratings == {"s1": 5}
        assert result["rated"] == 1

    def test_rated_favorite_is_left_alone(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1", rating=4)])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        worker.sync_once()

        assert library.ratings == {}


class TestFeedback:
    def test_sends_love_for_unseen_starred_song(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1")])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.sync_once()

        assert feedback.sent == [("mbid-1", 1)]
        assert result["synced"] == 1
        assert SyncStore(db).needs_feedback("s1", LOVE) is False

    def test_does_not_resend_for_synced_song(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        SyncStore(db).record("s1", LOVE, "mbid-1", lb_synced=1)
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1")])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        worker.sync_once()

        assert feedback.sent == []

    def test_retries_song_whose_feedback_failed(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1")])
        feedback = FakeFeedbackService(results={"mbid-1": False})
        worker = _make_worker(config, db, library, feedback)

        result = worker.sync_once()

        assert result["failed"] == 1
        assert SyncStore(db).needs_feedback("s1", LOVE) is True

        # Next cycle: a healthy ListenBrainz delivers it.
        feedback.results = {}
        result = worker.sync_once()
        assert result["synced"] == 1
        assert feedback.sent == [("mbid-1", 1), ("mbid-1", 1)]

    def test_mbid_less_song_is_recorded_as_synced(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", None)])
        feedback = FakeFeedbackService()
        worker = _make_worker(config, db, library, feedback)

        result = worker.sync_once()

        assert feedback.sent == []
        assert result["synced"] == 1
        # Never retried: nothing to send.
        assert SyncStore(db).needs_feedback("s1", LOVE) is False

    def test_disabled_listenbrainz_records_pending(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(starred=[_song("s1", "Track 1", "mbid-1")])
        feedback = FakeFeedbackService(disabled=True)
        worker = _make_worker(config, db, library, feedback)

        result = worker.sync_once()

        assert result["failed"] == 1
        # lb_synced=0: delivered whenever ListenBrainz is re-enabled.
        assert SyncStore(db).needs_feedback("s1", LOVE) is True
