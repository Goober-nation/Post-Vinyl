"""
Tests for SyncStore — SQLite persistence for love/hate sync state.
"""

import pytest

from app.db.database import Database
from app.db.sync_store import HATE, LOVE, SyncStore


def _make_config(tmpdir):
    from pathlib import Path

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmpdir) / "data")

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    return cfg


@pytest.fixture
def store(tmp_path):
    config = _make_config(str(tmp_path))
    db = Database(config)
    db.initialize_schema()
    yield SyncStore(db)
    db.close()


class TestRecord:
    def test_record_creates_row(self, store):
        store.record("song-1", LOVE, "mbid-1", lb_synced=1)
        row = store.get("song-1")
        assert row is not None
        assert row["song_type"] == LOVE
        assert row["mbid"] == "mbid-1"
        assert row["lb_synced"] == 1

    def test_record_is_an_upsert(self, store):
        store.record("song-1", LOVE, "mbid-1", lb_synced=1)
        store.record("song-1", HATE, "mbid-1", lb_synced=0)
        row = store.get("song-1")
        assert row["song_type"] == HATE
        assert row["lb_synced"] == 0


class TestNeedsFeedback:
    def test_no_row_needs_feedback(self, store):
        assert store.needs_feedback("unknown-song", LOVE) is True

    def test_synced_row_does_not(self, store):
        store.record("song-1", LOVE, "mbid-1", lb_synced=1)
        assert store.needs_feedback("song-1", LOVE) is False

    def test_failed_row_is_retried(self, store):
        store.record("song-1", LOVE, "mbid-1", lb_synced=0)
        assert store.needs_feedback("song-1", LOVE) is True

    def test_opposite_intent_needs_feedback(self, store):
        # A love row must not suppress a later hate and vice versa.
        store.record("song-1", LOVE, "mbid-1", lb_synced=1)
        assert store.needs_feedback("song-1", HATE) is True


class TestMarkFeedbackSynced:
    def test_marks_row_delivered(self, store):
        store.record("song-1", HATE, "mbid-1", lb_synced=0)
        store.mark_feedback_synced("song-1")
        assert store.needs_feedback("song-1", HATE) is False
