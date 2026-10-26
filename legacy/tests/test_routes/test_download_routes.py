"""
Integration tests for Download API routes (P4-2).

Uses FastAPI TestClient with a fake DownloadService injected via create_app().
Exercises the 4 endpoints plus the global error format:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

import tempfile
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.file_store import FileStore
from app.exceptions import (
    MaxRetriesExceededError,
    NoViablePeerError,
    SlskdConnectionError,
    TransferNotFoundError,
)
from app.main import create_app
from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    Transfer,
)


class FakeDownloadService(DownloadService):
    """In-memory DownloadService that raises real exceptions."""

    def __init__(self):
        self.transfers = {}
        self.next_id = 1
        self.fail_queue = False  # Simulate slskd connection failure
        self.partial_fail = False  # Simulate partial batch failure
        self.queue_calls: list[dict] = []
        # search_id -> peer responses, as slskd would serve them for retry
        self.search_responses: dict[str, list[dict]] = {}

    def _new_transfer(self, username, filename, size):
        transfer_id = f"transfer-{self.next_id}"
        self.next_id += 1
        transfer = Transfer(
            transfer_id=transfer_id,
            username=username,
            filename=filename,
            size=size,
            state="queued",
            progress=0.0,
            speed=None,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
        )
        self.transfers[transfer_id] = transfer
        return transfer

    def queue(self, username, files, search_id=None, destination=None) -> QueueResult:
        self.queue_calls.append(
            {"username": username, "files": files, "search_id": search_id}
        )
        if self.fail_queue:
            raise SlskdConnectionError("http://slskd:5030", "Connection refused")
        if self.partial_fail and len(files) > 1:
            # First file succeeds, rest fail
            self._new_transfer(username, files[0]["filename"], files[0].get("size", 0))
            failures = [
                {"filename": f["filename"], "message": "peer not found"}
                for f in files[1:]
            ]
            return QueueResult(
                enqueued_count=1,
                failures=failures,
                search_id=search_id,
            )
        for f in files:
            self._new_transfer(username, f["filename"], f.get("size", 0))
        return QueueResult(enqueued_count=len(files), failures=[], search_id=search_id)

    def get_status(self) -> list[Transfer]:
        return list(self.transfers.values())

    def fetch_search_responses(self, search_id: str) -> list[dict]:
        return list(self.search_responses.get(search_id, []))


    def cancel(self, transfer_id: str) -> bool:
        if transfer_id not in self.transfers:
            raise TransferNotFoundError(transfer_id)
        self.transfers[transfer_id].state = "cancelled"
        return True

    def delete_transfer(self, transfer_id: str) -> bool:
        if transfer_id not in self.transfers:
            return False
        del self.transfers[transfer_id]
        return True

    def get_transfer(self, transfer_id: str) -> Transfer:
        if transfer_id not in self.transfers:
            raise TransferNotFoundError(transfer_id)
        return self.transfers[transfer_id]


@pytest.fixture
def client():
    service = FakeDownloadService()
    app = create_app(download_service=service)
    return TestClient(app)


@pytest.fixture
def service(client):
    return client.app.state.services["download"]


def _app_with_db(tmpdir):
    """A test app backed by a real, initialised database in tmpdir."""

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = tmpdir

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths

    db = Database(cfg)
    db.initialize_schema()
    service = FakeDownloadService()
    app = create_app(download_service=service, database=db)
    return db, service, TestClient(app)


def _mark_completed(db, transfer_id, username="peer1", filename="song.mp3"):
    """What the monitor does when slskd reports a row's transfer completed:
    adopt it, then set it 'completed', which creates its file row as pending."""
    store = DownloadStore(db)
    row = store.adopt(username, filename, transfer_id)
    store.set_state(row["id"], "completed")


# ============================================================================
# GET /api/transfers
# ============================================================================


class TestListTransfers:
    def test_empty(self, client):
        """No transfers returns empty list."""
        resp = client.get("/api/transfers")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_float_speed_normalized(self, client, service):
        """slskd float averageSpeed is normalized to int bytes/sec."""
        service.transfers["t1"] = Transfer(
            transfer_id="t1",
            username="peer1",
            filename="song.mp3",
            size=1000,
            state="downloading",
            progress=50.0,
            speed=941839.2900798914,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
        )

        resp = client.get("/api/transfers")

        assert resp.status_code == 200
        assert resp.json()[0]["speed"] == 941839

    def test_completed_without_import_reports_importing(self):
        """slskd "completed" means the network transfer is done, not that the
        file is in the library yet. While the file is still pending placement
        the UI must say "importing"."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "song.mp3"}],
                    "search_id": "search-completed-1",
                },
            )
            transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]
            service.transfers[transfer_id].state = "completed"
            _mark_completed(db, transfer_id)

            resp = client.get("/api/transfers")

            assert resp.status_code == 200
            assert resp.json()[0]["state"] == "importing"

    def test_completed_with_import_handled_reports_completed(self):
        """Once the file has actually been placed, the real "completed" state
        is restored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "song.mp3"}],
                    "search_id": "search-completed-2",
                },
            )
            transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]
            service.transfers[transfer_id].state = "completed"
            _mark_completed(db, transfer_id)
            pending = FileStore(db).pending_for("peer1", "song.mp3")
            FileStore(db).mark_moved(pending["id"], "new-system-tests/song.mp3")

            resp = client.get("/api/transfers")

            assert resp.status_code == 200
            assert resp.json()[0]["state"] == "completed"

    def test_completed_with_no_downloads_row_reports_completed_not_importing(self):
        """slskd keeps its own transfer history independently of musica —
        a transfer_id musica has no downloads row for at all (e.g. slskd
        reporting a transfer from before musica's last reset) is not "still
        importing", it's simply not something musica is tracking. Downgrading
        it to "importing" would make it look permanently stuck forever,
        since nothing will ever mark an import musica never started as
        done."""
        with tempfile.TemporaryDirectory() as tmpdir:

            class MockPaths:
                pass

            paths = MockPaths()
            paths.data_dir = tmpdir

            class MockConfig:
                pass

            cfg = MockConfig()
            cfg.paths = paths

            db = Database(cfg)
            db.initialize_schema()

            service = FakeDownloadService()
            app = create_app(download_service=service, database=db)
            client = TestClient(app)

            client.post(
                "/api/queue",
                json={"username": "peer1", "search_id": "search-queue-test", "files": [{"filename": "song.mp3"}]},
            )
            transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]
            service.transfers[transfer_id].state = "completed"
            # Deliberately no downloads row inserted for this transfer_id.

            resp = client.get("/api/transfers")

            assert resp.status_code == 200
            assert resp.json()[0]["state"] == "completed"

    def test_with_transfers(self, client):
        """Returns transfer fields."""
        client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3", "size": 5242880}],
            },
        )

        resp = client.get("/api/transfers")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        t = body[0]
        assert t["username"] == "peer1"
        assert t["filename"] == "song.mp3"
        assert t["size"] == 5242880
        assert t["state"] == "queued"
        assert t["progress"] == 0.0
        assert t["speed"] is None
        assert t["started_at"]
        assert t["completed_at"] is None
        assert t["is_rec_download"] is False


# ============================================================================
# POST /api/queue
# ============================================================================


class TestQueue:
    def test_queue_success(self, client):
        """Valid request returns 201 with enqueued count."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "files": [{"filename": "song.mp3", "size": 5242880}],
                "search_id": "search-1",
            },
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["enqueued_count"] == 1
        assert body["failures"] == []
        assert body["search_id"] == "search-1"

    def test_queue_partial_success_207(self, client, service):
        """Partial batch failure returns 207 with failures."""
        service.partial_fail = True

        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [
                    {"filename": "ok.mp3", "size": 100},
                    {"filename": "bad.mp3", "size": 200},
                ],
            },
        )

        assert resp.status_code == 207
        body = resp.json()
        assert body["enqueued_count"] == 1
        assert len(body["failures"]) == 1
        assert body["failures"][0]["filename"] == "bad.mp3"

    def test_queue_destination_within_allowed_dir_succeeds(self, client):
        """A destination under the derived Discovery parent is accepted."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3", "size": 100}],
                "destination": "/music/Discovery/peer1",
            },
        )
        assert resp.status_code == 201

    def test_queue_destination_traversal_rejected(self, client):
        """A destination escaping the configured download dirs is rejected."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3", "size": 100}],
                "destination": "/music/downloads/../../etc",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DESTINATION"

    def test_queue_missing_username(self, client):
        """Missing username returns 400 VALIDATION_ERROR."""
        resp = client.post("/api/queue", json={"files": [{"filename": "song.mp3"}]})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_queue_without_search_id_400(self, client, service):
        """Every download is tracked against the search it came from, so a
        queue request with no search_id is rejected and nothing is sent to
        slskd."""
        resp = client.post(
            "/api/queue",
            json={"username": "peer1", "files": [{"filename": "song.mp3"}]},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert service.queue_calls == []

    def test_queue_blank_search_id_400(self, client, service):
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "files": [{"filename": "song.mp3"}],
                "search_id": "   ",
            },
        )

        assert resp.status_code == 400
        assert service.queue_calls == []

    def test_queue_blank_username(self, client):
        """Whitespace-only username returns 400."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "   ",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3"}],
            },
        )

        assert resp.status_code == 400

    def test_queue_empty_files(self, client):
        """Empty files list returns 400."""
        resp = client.post("/api/queue", json={"username": "peer1", "search_id": "search-queue-test", "files": []})

        assert resp.status_code == 400

    def test_queue_missing_filename(self, client):
        """File without filename returns 400."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"size": 100}],
            },
        )

        assert resp.status_code == 400

    def test_queue_negative_size(self, client):
        """Negative size returns 400."""
        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3", "size": -5}],
            },
        )

        assert resp.status_code == 400

    def test_queue_connection_error(self, client, service):
        """slskd connection failure returns 503 SLSKD_CONNECTION_FAILED."""
        service.fail_queue = True

        resp = client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3"}],
            },
        )

        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "SLSKD_CONNECTION_FAILED"

    def test_queue_persists_pending_row_in_db(self, tmp_path):
        """Successful queue with search_id persists a pending download row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Build config-worthy object with data_dir pointing at tmpdir
            class MockPaths:
                pass

            paths = MockPaths()
            paths.data_dir = tmpdir

            class MockConfig:
                pass

            cfg = MockConfig()
            cfg.paths = paths

            db = Database(cfg)
            db.initialize_schema()

            service = FakeDownloadService()
            app = create_app(download_service=service, database=db)
            client = TestClient(app)

            resp = client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "search_id": "search-persist-1",
                },
            )
            assert resp.status_code == 201

            store = DownloadStore(db)
            rows = store.get_transfers_by_state("queued")
            assert len(rows) == 1
            assert rows[0]["username"] == "peer1"
            assert rows[0]["filename"] == "song.mp3"
            assert rows[0]["search_id"] == "search-persist-1"

            db.close()


# ============================================================================
# POST /api/queue/retry/{transfer_id}
# ============================================================================


PEER1_RESPONSE = {
    "username": "peer1",
    "hasFreeUploadSlot": True,
    "files": [{"filename": "song.mp3", "size": 100}],
}
PEER2_RESPONSE = {
    "username": "peer2",
    "hasFreeUploadSlot": True,
    "files": [{"filename": "song.mp3", "size": 100}],
}


def _failed_download(client, service, search_id="search-retry-1", responses=None):
    """Queue song.mp3 from peer1 and make slskd report it failed.

    Returns slskd's transfer id, which is what the Retry button sends.
    """
    client.post(
        "/api/queue",
        json={
            "username": "peer1",
            "files": [{"filename": "song.mp3", "size": 100}],
            "search_id": search_id,
        },
    )
    transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]
    # The monitor adopts slskd's transfer into the queued row before the UI
    # can show it, so the Retry button only ever sees adopted rows.
    DownloadStore(client.app.state.db).adopt("peer1", "song.mp3", transfer_id)
    service.transfers[transfer_id].state = "failed"
    service.search_responses[search_id] = (
        [PEER1_RESPONSE, PEER2_RESPONSE] if responses is None else responses
    )
    return transfer_id


class TestRetry:
    def test_retry_requeues_the_same_row_from_another_peer(self):
        """The Retry button runs the monitor's DB-backed retry: the download
        keeps its one row, moves to a peer it hasn't tried, and the failed
        slskd transfer is removed so it cannot land as a duplicate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            transfer_id = _failed_download(client, service)

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["message"] == "Retrying from peer2"
            assert body["new_transfer_id"] is None

            rows = db.fetch_all("SELECT id, username, state FROM downloads")
            assert len(rows) == 1
            assert rows[0]["username"] == "peer2"
            assert rows[0]["state"] == "queued"
            assert DownloadStore(db).retry_count(rows[0]["id"]) == 1

            assert service.queue_calls[-1]["username"] == "peer2"
            assert service.queue_calls[-1]["search_id"] == "search-retry-1"
            assert transfer_id not in service.transfers
            db.close()

    def test_retry_button_bypasses_the_auto_retry_manual_gate(self):
        """auto_retry_manual_soulseek is off by default, which only stops
        the automatic path. The button is an explicit request and still
        retries a manual download."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            assert client.app.state.config.download.auto_retry_manual_soulseek is False
            transfer_id = _failed_download(client, service)

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 200
            assert service.queue_calls[-1]["username"] == "peer2"
            db.close()

    def test_retry_never_tries_a_peer_twice(self):
        """Every peer the row has been queued from is skipped: once peer2
        has failed too, the only peer left is peer1, already tried."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            transfer_id = _failed_download(client, service)
            assert client.post(f"/api/queue/retry/{transfer_id}").status_code == 200

            peer2_transfer = next(
                t for t in client.get("/api/transfers").json() if t["username"] == "peer2"
            )
            DownloadStore(db).adopt("peer2", "song.mp3", peer2_transfer["transfer_id"])
            service.transfers[peer2_transfer["transfer_id"]].state = "failed"

            resp = client.post(f"/api/queue/retry/{peer2_transfer['transfer_id']}")

            assert resp.status_code == 500
            assert resp.json()["error"]["code"] == "NO_VIABLE_PEER"
            assert [c["username"] for c in service.queue_calls] == ["peer1", "peer2"]
            db.close()

    def test_retry_budget_exhausted_500(self):
        """Once max_retries_per_track retries are used up (default 3), the
        button refuses with MAX_RETRIES_EXCEEDED."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            transfer_id = _failed_download(client, service)
            row = DownloadStore(db).get_by_slskd_id(transfer_id)
            for n in range(3):
                db.execute(
                    "INSERT INTO download_attempts (download_id, username, attempted_at) "
                    "VALUES (?, ?, ?)",
                    (row["id"], f"extra-peer-{n}", int(time.time())),
                )

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 500
            assert resp.json()["error"]["code"] == "MAX_RETRIES_EXCEEDED"
            assert len(service.queue_calls) == 1
            db.close()

    def test_retry_no_viable_peer_500(self):
        """Only the peer that already failed is in the search: no alternative."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            transfer_id = _failed_download(client, service, responses=[PEER1_RESPONSE])

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 500
            assert resp.json()["error"]["code"] == "NO_VIABLE_PEER"
            db.close()

    def test_retry_with_no_search_responses_500(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            transfer_id = _failed_download(client, service, responses=[])

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 500
            assert resp.json()["error"]["code"] == "NO_VIABLE_PEER"
            db.close()

    def test_retry_not_found(self, client):
        """Unknown transfer returns 404 TRANSFER_NOT_FOUND."""
        resp = client.post("/api/queue/retry/nope")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TRANSFER_NOT_FOUND"

    def test_retry_not_found_when_slskd_id_has_no_row(self):
        """A transfer slskd reports but this app never started has no row,
        so there is nothing to retry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            service._new_transfer("peer9", "other.mp3", 100)
            transfer_id = next(iter(service.transfers))
            service.transfers[transfer_id].state = "failed"

            resp = client.post(f"/api/queue/retry/{transfer_id}")

            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "TRANSFER_NOT_FOUND"
            db.close()


# ============================================================================
# DELETE /api/transfers/{transfer_id}
# ============================================================================


class TestCancel:
    def test_cancel_success(self, client):
        """Cancelling a transfer returns cancelled=true."""
        client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [{"filename": "song.mp3"}],
            },
        )
        transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]

        resp = client.delete(f"/api/transfers/{transfer_id}")

        assert resp.status_code == 200
        assert resp.json() == {"transfer_id": transfer_id, "cancelled": True}

    def test_cancel_not_found(self, client):
        """Unknown transfer returns 404."""
        resp = client.delete("/api/transfers/nope")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TRANSFER_NOT_FOUND"


# ============================================================================
# DELETE /api/transfers?state=finished
# ============================================================================


class TestDeleteFinished:
    def test_removes_only_finished_states(self, client, service):
        """Only completed/failed/cancelled transfers are deleted; queued survives."""
        client.post(
            "/api/queue",
            json={
                "username": "peer1",
                "search_id": "search-queue-test",
                "files": [
                    {"filename": "a.mp3"},
                    {"filename": "b.mp3"},
                    {"filename": "c.mp3"},
                ],
            },
        )
        ids = [t["transfer_id"] for t in client.get("/api/transfers").json()]
        service.transfers[ids[0]].state = "completed"
        service.transfers[ids[1]].state = "failed"
        # ids[2] stays "queued"

        resp = client.delete("/api/transfers?state=finished")

        assert resp.status_code == 200
        assert resp.json() == {"deleted_count": 2}

        remaining_ids = {t["transfer_id"] for t in client.get("/api/transfers").json()}
        assert remaining_ids == {ids[2]}

    def test_no_finished_transfers_returns_zero(self, client):
        resp = client.delete("/api/transfers?state=finished")

        assert resp.status_code == 200
        assert resp.json() == {"deleted_count": 0}

    def test_missing_state_param_400(self, client):
        resp = client.delete("/api/transfers")

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_state_value_400(self, client):
        resp = client.delete("/api/transfers?state=active")

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_local_db_row_removed_regardless_of_slskd_outcome(self, tmp_path):
        """Local DB row is deleted even if the fake slskd-side delete fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, client = _app_with_db(tmpdir)
            client.post(
                "/api/queue",
                json={
                    "username": "peer1",
                    "files": [{"filename": "song.mp3"}],
                    "search_id": "search-delete-1",
                },
            )
            transfer_id = client.get("/api/transfers").json()[0]["transfer_id"]
            service.transfers[transfer_id].state = "completed"
            # A completed download whose file has been placed is eligible for
            # deletion. (A still-pending import is deliberately kept; that is
            # covered by the import guard, not by this test.)
            _mark_completed(db, transfer_id)
            pending = FileStore(db).pending_for("peer1", "song.mp3")
            FileStore(db).mark_moved(pending["id"], "new-system-tests/song.mp3")

            # Force the fake service's delete_transfer to fail (simulates a
            # slskd-side error) — local row must still be removed.
            original_delete = service.delete_transfer
            service.delete_transfer = lambda tid: False

            resp = client.delete("/api/transfers?state=finished")

            assert resp.status_code == 200
            assert resp.json() == {"deleted_count": 1}

            store = DownloadStore(db)
            assert store.get_by_slskd_id(transfer_id) is None

            service.delete_transfer = original_delete
            db.close()

    def test_queue_emits_transfer_queued(self):
        import asyncio

        import httpx

        async def _run():
            service = FakeDownloadService()
            app = create_app(download_service=service)
            hub = app.state.event_hub
            sub = hub.subscribe({"transfer"})

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/queue",
                    json={"username": "peer1", "search_id": "search-queue-test", "files": [{"filename": "song.mp3"}]},
                )
                assert resp.status_code == 201

            event = await asyncio.wait_for(sub.queue.get(), 2.0)
            assert event.event_type == "transfer.queued"
            assert event.data["username"] == "peer1"
            hub.unsubscribe(sub)

        asyncio.run(_run())

    def test_retry_emits_transfer_retried(self):
        import asyncio

        import httpx

        with tempfile.TemporaryDirectory() as tmpdir:
            db, service, sync_client = _app_with_db(tmpdir)
            app = sync_client.app
            hub = app.state.event_hub
            transfer_id = _failed_download(sync_client, service)

            async def _run():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    sub = hub.subscribe({"transfer"})
                    resp = await client.post(f"/api/queue/retry/{transfer_id}")
                    assert resp.status_code == 200

                event = await asyncio.wait_for(sub.queue.get(), 2.0)
                assert event.event_type == "transfer.retried"
                assert event.data["success"] is True
                hub.unsubscribe(sub)

            asyncio.run(_run())
            db.close()

    def test_cancel_emits_transfer_cancelled(self):
        import asyncio

        import httpx

        async def _run():
            service = FakeDownloadService()
            app = create_app(download_service=service)
            hub = app.state.event_hub

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await client.post(
                    "/api/queue",
                    json={"username": "peer1", "search_id": "search-queue-test", "files": [{"filename": "song.mp3"}]},
                )
                transfer_id = (await client.get("/api/transfers")).json()[0][
                    "transfer_id"
                ]

                sub = hub.subscribe({"transfer"})
                resp = await client.delete(f"/api/transfers/{transfer_id}")
                assert resp.status_code == 200

            event = await asyncio.wait_for(sub.queue.get(), 2.0)
            assert event.event_type == "transfer.cancelled"
            assert event.data["cancelled"] is True
            hub.unsubscribe(sub)

        asyncio.run(_run())

    def test_delete_finished_emits_transfer_deleted(self):
        import asyncio

        import httpx

        async def _run():
            service = FakeDownloadService()
            app = create_app(download_service=service)
            hub = app.state.event_hub

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                await client.post(
                    "/api/queue",
                    json={"username": "peer1", "search_id": "search-queue-test", "files": [{"filename": "song.mp3"}]},
                )
                transfer_id = (await client.get("/api/transfers")).json()[0][
                    "transfer_id"
                ]
                service.transfers[transfer_id].state = "completed"

                sub = hub.subscribe({"transfer"})
                resp = await client.delete("/api/transfers?state=finished")
                assert resp.status_code == 200

            event = await asyncio.wait_for(sub.queue.get(), 2.0)
            assert event.event_type == "transfer.deleted"
            assert event.data["deleted_count"] == 1
            hub.unsubscribe(sub)

        asyncio.run(_run())
