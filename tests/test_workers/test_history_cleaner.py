"""
Tests for the HistoryCleaner worker (P6.9-7): periodic clearing of slskd's
terminal transfer history.
"""

from datetime import datetime, timezone

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.services.interfaces.download import Transfer
from app.workers.history_cleaner import HistoryCleaner


def _make_config(interval_minutes=15, tmpdir=None):
    from pathlib import Path

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(Path(tmpdir) / "data") if tmpdir else None

    class MockDownload:
        pass

    download = MockDownload()
    download.history_clear_interval_minutes = interval_minutes

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.download = download
    return cfg


def _transfer(tid, username, filename, state, size=1000):
    return Transfer(
        transfer_id=tid,
        username=username,
        filename=filename,
        size=size,
        state=state,
        progress=100.0 if state == "completed" else 0.0,
        speed=None,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc)
        if state in ("completed", "failed", "cancelled")
        else None,
    )


class FakeDownloadService:
    def __init__(self, transfers=None, uploads=None, delete_result=True):
        self.transfers = list(transfers or [])
        self.uploads = list(uploads or [])
        self.delete_result = delete_result
        self.deleted: list[str] = []
        self.upload_deleted: list[tuple[str, str]] = []

    def get_status(self):
        return list(self.transfers)

    def delete_transfer(self, transfer_id):
        if not self.delete_result:
            return False
        self.deleted.append(transfer_id)
        return True

    def get_upload_status(self):
        return list(self.uploads)

    def delete_upload_transfer(self, transfer_id, username):
        self.upload_deleted.append((transfer_id, username))
        return True


class FakeEventHub:
    def __init__(self):
        self.events = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))


@pytest.fixture
def db(tmp_path):
    config = _make_config(tmpdir=str(tmp_path))
    database = Database(config)
    database.initialize_schema()
    yield database
    database.close()


def _make_cleaner(config, download, database=None, event_hub=None):
    return HistoryCleaner(config, download, database=database, event_hub=event_hub)


class TestDownloadCleanup:
    def test_clears_terminal_downloads_only(self):
        service = FakeDownloadService(
            transfers=[
                _transfer("t-done", "peer1", "a.mp3", "completed"),
                _transfer("t-fail", "peer1", "b.mp3", "failed"),
                _transfer("t-cancel", "peer2", "c.mp3", "cancelled"),
                _transfer("t-active", "peer2", "d.mp3", "downloading"),
                _transfer("t-queued", "peer3", "e.mp3", "queued"),
            ]
        )
        cleaner = _make_cleaner(_make_config(), service)

        result = cleaner.clean_once()

        assert result["deleted_downloads"] == 3
        assert result["failed_downloads"] == 0
        assert set(service.deleted) == {"t-done", "t-fail", "t-cancel"}

    def test_skips_completed_still_awaiting_import(self, db):
        store = DownloadStore(db)
        store.insert_pending("search-1", "peer1", "song.mp3", 1000, False)
        store.upsert_transfer(
            _transfer("t-importing", "peer1", "song.mp3", "completed", size=1000)
        )
        service = FakeDownloadService(
            transfers=[_transfer("t-importing", "peer1", "song.mp3", "completed")]
        )
        cleaner = _make_cleaner(_make_config(), service, database=db)

        result = cleaner.clean_once()

        assert result["skipped"] == 1
        assert result["deleted_downloads"] == 0
        assert service.deleted == []

    def test_keeps_local_rows(self, db):
        store = DownloadStore(db)
        store.insert_pending("search-2", "peer2", "gone.mp3", 1000, False)
        store.upsert_transfer(
            _transfer("t-gone", "peer2", "gone.mp3", "failed", size=1000)
        )
        service = FakeDownloadService(
            transfers=[_transfer("t-gone", "peer2", "gone.mp3", "failed")]
        )
        cleaner = _make_cleaner(_make_config(), service, database=db)

        cleaner.clean_once()

        assert service.deleted == ["t-gone"]
        row = db.fetch_one("SELECT state FROM downloads WHERE username = ?", ("peer2",))
        assert row is not None
        assert row["state"] == "failed"

    def test_failed_delete_is_counted_and_retried_next_cycle(self):
        service = FakeDownloadService(
            transfers=[_transfer("t-sticky", "peer1", "x.mp3", "failed")],
            delete_result=False,
        )
        cleaner = _make_cleaner(_make_config(), service)

        result = cleaner.clean_once()

        assert result["failed_downloads"] == 1
        assert result["deleted_downloads"] == 0
        assert service.deleted == []

    def test_slskd_unreachable_degrades_to_warning(self):
        class BrokenService(FakeDownloadService):
            def get_status(self):
                raise RuntimeError("slskd down")

        cleaner = _make_cleaner(_make_config(), BrokenService())

        result = cleaner.clean_once()

        assert result["deleted_downloads"] == 0
        assert result["failed_downloads"] == 0


class TestUploadCleanup:
    def test_clears_terminal_uploads_best_effort(self):
        service = FakeDownloadService(
            uploads=[
                _transfer("u-done", "peer1", "a.mp3", "completed"),
                _transfer("u-fail", "peer2", "b.mp3", "failed"),
                _transfer("u-active", "peer3", "c.mp3", "downloading"),
            ]
        )
        cleaner = _make_cleaner(_make_config(), service)

        result = cleaner.clean_once()

        assert result["deleted_uploads"] == 2
        assert set(service.upload_deleted) == {("u-done", "peer1"), ("u-fail", "peer2")}

    def test_missing_upload_surface_leaves_uploads_alone(self):
        service = FakeDownloadService(
            uploads=[_transfer("u-done", "peer1", "a.mp3", "completed")]
        )
        service.get_upload_status = None
        service.delete_upload_transfer = None
        cleaner = _make_cleaner(_make_config(), service)

        result = cleaner.clean_once()

        assert result["deleted_uploads"] == 0
        assert result["deleted_downloads"] == 0


class TestInterval:
    def test_interval_reads_config(self):
        assert (
            _make_cleaner(_make_config(7), FakeDownloadService())._interval_minutes()
            == 7
        )

    def test_interval_zero_disables(self):
        assert (
            _make_cleaner(_make_config(0), FakeDownloadService())._interval_minutes()
            == 0
        )


class TestEvent:
    def test_publishes_summary_event(self):
        service = FakeDownloadService(
            transfers=[_transfer("t-done", "peer1", "a.mp3", "completed")]
        )
        hub = FakeEventHub()
        cleaner = _make_cleaner(_make_config(), service, event_hub=hub)

        cleaner.clean_once()

        assert hub.events[0][0] == "system.history_cleaned"
        assert hub.events[0][1]["deleted_downloads"] == 1
