"""
Lifecycle tests for downloads this app started (migration 015 model).

These cover the phase-4 exit criteria on rows the app wrote itself:
- a completed download creates exactly one file_management row and is moved;
- a file that never appears is given up on and not retried;
- three dead peers in a row exhaust the retry budget, failing the download,
  and no peer is tried twice;
- an slskd transfer with no row of ours is ignored;
- the queue route writes its download rows before asking slskd to queue.

Unlike test_download_monitor.py, every transfer here has a row, because that
is the only kind the monitor is meant to act on.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.file_store import FileStore
from app.exceptions import SlskdConnectionError
from app.main import create_app
from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    Transfer,
)
from app.workers.download_monitor import DownloadMonitor

FILE = "a\\b\\01 - Track.mp3"
PEERS_RESPONSE_FILE = "music\\Band\\track.mp3"


class _Paths:
    pass


class _Download:
    check_interval = 15
    max_retries_per_track = 3
    peer_ban_days = 2
    auto_retry_manual_soulseek = True
    bad_peer_threshold = 3
    missing_source_timeout_minutes = 5


def _config(tmpdir, **download):
    root = Path(tmpdir)
    paths = _Paths()
    paths.data_dir = str(root / "data")
    paths.download_path = root / "download"
    paths.library_path = root / "library"

    dl = _Download()
    for key, value in download.items():
        setattr(dl, key, value)

    class _Config:
        pass

    cfg = _Config()
    cfg.paths = paths
    cfg.download = dl
    return cfg


class _Service:
    """Minimal slskd stand-in for the monitor."""

    def __init__(self, transfers=None):
        self.transfers = list(transfers or [])
        self.queue_calls: list[dict] = []
        self.responses: dict[str, list[dict]] = {}
        self.deleted: list[str] = []

    def get_status(self):
        return list(self.transfers)

    def queue(self, username, files, search_id=None, destination=None):
        self.queue_calls.append({"username": username, "files": files})
        return QueueResult(enqueued_count=len(files), failures=[], search_id=search_id)

    def fetch_search_responses(self, search_id):
        return self.responses.get(search_id, [])

    def delete_transfer(self, transfer_id):
        self.deleted.append(transfer_id)
        return True


class _Library:
    def __init__(self):
        self.scans = 0

    def trigger_scan(self):
        self.scans += 1
        return True


class _Hub:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, event_type, data):
        self.events.append((event_type, data))


def _transfer(tid, username, filename, state, size=5000):
    return Transfer(
        transfer_id=tid,
        username=username,
        filename=filename,
        size=size,
        state=state,
        progress=100.0 if state == "completed" else 0.0,
        speed=None,
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )


def _db(cfg):
    database = Database(cfg)
    database.initialize_schema()
    return database


def _source(cfg, username, *parts):
    path = Path(cfg.paths.download_path, "complete", "soulseek", username, *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fake audio")
    return path


class TestTrackedCompletion:
    def test_completed_download_gets_one_file_row_and_is_moved(self, tmp_path):
        cfg = _config(tmp_path)
        db = _db(cfg)
        try:
            store = DownloadStore(db)
            store.insert_pending("s-life", "peerone", FILE, 5000, False, "mbid-1")
            source = _source(cfg, "peerone", "a", "b", "01 - Track.mp3")
            svc = _Service([_transfer("t-life", "peerone", FILE, "completed")])
            lib = _Library()
            hub = _Hub()
            monitor = DownloadMonitor(cfg, svc, lib, db, hub, interval=15)

            result = monitor.poll_once()

            target = cfg.paths.library_path / "new-system-tests" / "01 - Track.mp3"
            assert result["moved"] == [str(target)]
            assert target.exists() and not source.exists()
            assert lib.scans == 1

            rows = db.fetch_all("SELECT * FROM file_management")
            assert len(rows) == 1
            assert rows[0]["stage"] == "moved"
            assert rows[0]["placed_path"] == "new-system-tests/01 - Track.mp3"
            assert rows[0]["username"] == "peerone"
            provenance = db.fetch_all("SELECT mb_recording_id FROM mb_provenance")
            assert [p["mb_recording_id"] for p in provenance] == ["mbid-1"]

            completed = [d for name, d in hub.events if name == "transfer.completed"]
            assert len(completed) == 1
            assert completed[0]["target_dir"] == str(target.parent)

            # Later polls keep seeing "completed" from slskd: no second row,
            # no second move.
            again = monitor.poll_once()
            assert again["moved"] == []
            assert len(db.fetch_all("SELECT * FROM file_management")) == 1
        finally:
            db.close()

    def test_file_that_never_appears_is_given_up_and_not_retried(self, tmp_path):
        cfg = _config(tmp_path, missing_source_timeout_minutes=5)
        db = _db(cfg)
        try:
            DownloadStore(db).insert_pending("s-ghost", "ghost", FILE, 5000, False)
            svc = _Service([_transfer("t-ghost", "ghost", FILE, "completed")])
            monitor = DownloadMonitor(cfg, svc, _Library(), db, _Hub(), interval=15)

            # First miss starts the clock; pretend it has run past the timeout.
            monitor.poll_once()
            monitor._missing_source_since["t-ghost"] = time.time() - 301
            result = monitor.poll_once()

            assert result["moved"] == []
            row = db.fetch_one("SELECT stage FROM file_management")
            assert row["stage"] == "missing"
            assert FileStore(db).pending_for("ghost", FILE) is None

            # slskd still says completed; nothing is retried or moved any more.
            assert monitor.poll_once()["moved"] == []
            assert db.fetch_one("SELECT stage FROM file_management")["stage"] == "missing"
        finally:
            db.close()


class TestRetryBudget:
    def test_three_dead_peers_exhaust_the_budget_and_no_peer_is_tried_twice(
        self, tmp_path
    ):
        cfg = _config(tmp_path, max_retries_per_track=2, bad_peer_threshold=5)
        db = _db(cfg)
        try:
            store = DownloadStore(db)
            download_id = store.insert_pending("s-budget", "peerA", PEERS_RESPONSE_FILE, 100, False)
            svc = _Service()
            svc.responses["s-budget"] = [
                {
                    "username": peer,
                    "hasFreeUploadSlot": True,
                    "files": [{"filename": PEERS_RESPONSE_FILE, "size": 100}],
                }
                for peer in ("peerA", "peerB", "peerC", "peerD")
            ]
            hub = _Hub()
            monitor = DownloadMonitor(cfg, svc, _Library(), db, hub, interval=15)

            # Each poll reports the current attempt failed.
            svc.transfers = [_transfer("tA", "peerA", PEERS_RESPONSE_FILE, "failed")]
            monitor.poll_once()
            svc.transfers = [_transfer("tB", "peerB", PEERS_RESPONSE_FILE, "failed")]
            monitor.poll_once()
            svc.transfers = [_transfer("tC", "peerC", PEERS_RESPONSE_FILE, "failed")]
            monitor.poll_once()

            assert [c["username"] for c in svc.queue_calls] == ["peerB", "peerC"]
            assert store.tried_peers(download_id) == {"peerA", "peerB", "peerC"}
            assert store.retry_count(download_id) == 2
            row = store.get(download_id)
            assert row["state"] == "failed"

            failed = [d for name, d in hub.events if name == "transfer.failed"]
            assert failed[-1]["will_retry"] is False
            assert "Max retries exceeded" in failed[-1]["error"]
        finally:
            db.close()


class TestUntrackedTransfersIgnored:
    def test_slskd_transfer_without_a_row_is_ignored(self, tmp_path):
        cfg = _config(tmp_path)
        db = _db(cfg)
        try:
            source = _source(cfg, "stranger", "a", "b", "01 - Track.mp3")
            svc = _Service([_transfer("t-stranger", "stranger", FILE, "completed")])
            hub = _Hub()
            monitor = DownloadMonitor(cfg, svc, _Library(), db, hub, interval=15)

            result = monitor.poll_once()

            assert result["transfers_seen"] == 1
            assert result["moved"] == []
            assert source.exists()
            assert db.fetch_one("SELECT COUNT(*) AS n FROM downloads")["n"] == 0
            assert db.fetch_one("SELECT COUNT(*) AS n FROM file_management")["n"] == 0
            assert hub.events == []
        finally:
            db.close()


class _RouteService(DownloadService):
    """slskd stand-in for the queue route that checks the DB at queue time."""

    def __init__(self, db=None):
        self._db = db
        self.rows_seen_at_queue = None
        self.failures: list[dict] = []
        self.raise_error = False

    def queue(self, username, files, search_id=None, destination=None):
        if self._db is not None:
            self.rows_seen_at_queue = len(
                DownloadStore(self._db).get_transfers_by_state("queued")
            )
        if self.raise_error:
            raise SlskdConnectionError("http://slskd:5030", "Connection refused")
        return QueueResult(
            enqueued_count=len(files) - len(self.failures),
            failures=list(self.failures),
            search_id=search_id,
        )

    def get_status(self):
        return []

    def retry(self, transfer_id):
        raise NotImplementedError

    def get_transfer(self, transfer_id):
        raise NotImplementedError

    def cancel(self, transfer_id):
        return False

    def delete_transfer(self, transfer_id):
        return False


class TestQueueWritesRowsFirst:
    def _client(self, tmp_path, service):
        cfg = _config(tmp_path)
        db = _db(cfg)
        app = create_app(download_service=service, database=db)
        return db, TestClient(app)

    def test_rows_exist_before_slskd_is_asked_to_queue(self, tmp_path):
        service = _RouteService()
        db, client = self._client(tmp_path, service)
        service._db = db
        try:
            resp = client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "search_id": "s-route-1",
                },
            )

            assert resp.status_code == 201
            assert service.rows_seen_at_queue == 1
            row = db.fetch_one("SELECT state, search_id FROM downloads")
            assert row["state"] == "queued"
            assert row["search_id"] == "s-route-1"
            attempt = db.fetch_one("SELECT success FROM download_attempts")
            assert attempt["success"] == 1
        finally:
            db.close()

    def test_file_slskd_refuses_is_marked_failed(self, tmp_path):
        service = _RouteService()
        db, client = self._client(tmp_path, service)
        service._db = db
        service.failures = [{"filename": "b.mp3", "message": "peer not found"}]
        try:
            resp = client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "a.mp3"}, {"filename": "b.mp3"}],
                    "search_id": "s-route-2",
                },
            )

            assert resp.status_code == 207
            states = {
                r["filename"]: r["state"]
                for r in db.fetch_all("SELECT filename, state FROM downloads")
            }
            assert states == {"a.mp3": "queued", "b.mp3": "failed"}
        finally:
            db.close()

    def test_unreachable_slskd_marks_the_rows_failed(self, tmp_path):
        service = _RouteService()
        db, client = self._client(tmp_path, service)
        service._db = db
        service.raise_error = True
        try:
            resp = client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "a.mp3"}],
                    "search_id": "s-route-3",
                },
            )

            assert resp.status_code == 503
            row = db.fetch_one("SELECT state FROM downloads")
            assert row["state"] == "failed"
        finally:
            db.close()
