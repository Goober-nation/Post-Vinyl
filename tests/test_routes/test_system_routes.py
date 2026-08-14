"""
Integration tests for System API routes (P4-6).

Tests GET /api/system/status, GET /api/logs, and
POST /api/system/stop-slskd-activity via TestClient.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.logging_config import RingBufferHandler
from app.main import create_app
from app.services.health import ServiceHealth
from app.services.interfaces.download import (
    DownloadService,
    QueueResult,
    RetryResult,
    Transfer,
)
from app.services.interfaces.search import SearchJob, SearchResult, SearchService


def _write_minimal_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """\
[server]
port = 8000
host = "0.0.0.0"

[logging]
level = "INFO"
format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
"""
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _write_minimal_config(tmp_path)
    cfg = Config(config_path=str(tmp_path / "config.toml"))
    cfg.load()
    app = create_app(config=cfg)
    return TestClient(app)


# ============================================================================
# GET /api/system/ping
# ============================================================================


class TestSystemPing:
    """Liveness must not depend on anything but the process being alive.

    The whole reason /api/system/ping exists is that /api/system/status
    live-checks slskd and Navidrome, so probes with a short timeout read
    third-party latency as "musica is down". A ping that ever grew a
    dependency would quietly reintroduce that, so these tests pin the
    property rather than the response body.
    """

    def test_ping_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/api/system/ping")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_ping_does_not_touch_backend_services(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No health check of any kind, however slow those services are."""
        calls: list[str] = []

        def _boom(*args: object, **kwargs: object) -> None:
            calls.append("called")
            raise AssertionError("ping must not run health checks")

        monkeypatch.setattr("app.services.health.check_all", _boom)
        monkeypatch.setattr("app.services.health.check_slskd", _boom)
        monkeypatch.setattr("app.services.health.check_navidrome", _boom)

        assert client.get("/api/system/ping").status_code == 200
        assert calls == []

    def test_ping_is_async_so_it_bypasses_the_worker_threadpool(self) -> None:
        """A sync handler would queue behind blocked sync endpoints on the
        anyio threadpool — i.e. stop answering exactly when liveness starts
        mattering. Assert the coroutine-ness directly; nothing else can."""
        import inspect

        from app.routes.system import system_ping

        assert inspect.iscoroutinefunction(system_ping)


# ============================================================================
# GET /api/system/status
# ============================================================================


class TestSystemStatus:
    def test_all_up(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        up = ServiceHealth(name="slskd", status="up", latency_ms=42)
        down = ServiceHealth(
            name="navidrome", status="down", latency_ms=100, error="timeout"
        )
        disabled = ServiceHealth(name="listenbrainz", status="disabled")

        monkeypatch.setattr(
            "app.routes.system.health.check_all",
            lambda config, **kw: {
                "slskd": up,
                "navidrome": down,
                "listenbrainz": disabled,
            },
        )

        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        body = resp.json()

        assert body["version"] == "0.1.0"
        assert body["uptime_seconds"] >= 0

        services = body["services"]
        assert services["slskd"]["status"] == "up"
        assert services["slskd"]["latency_ms"] == 42
        assert services["slskd"]["error"] is None

        assert services["navidrome"]["status"] == "down"
        assert services["navidrome"]["latency_ms"] == 100
        assert services["navidrome"]["error"] == "timeout"

        assert services["listenbrainz"]["status"] == "disabled"
        assert services["listenbrainz"]["latency_ms"] is None
        assert services["listenbrainz"]["error"] is None

    def test_error_field_null_when_up(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        up = ServiceHealth(name="slskd", status="up", latency_ms=10)

        monkeypatch.setattr(
            "app.routes.system.health.check_all",
            lambda config, **kw: {
                "slskd": up,
                "navidrome": up,
                "listenbrainz": ServiceHealth(name="lb", status="disabled"),
            },
        )

        resp = client.get("/api/system/status")
        assert resp.status_code == 200
        body = resp.json()

        for svc in body["services"].values():
            if svc["status"] == "up":
                assert svc["error"] is None


# ============================================================================
# POST /api/system/slskd/reconnect
# ============================================================================


class TestSlskdReconnect:
    def test_success(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.routes.system.health.reconnect_slskd", lambda config: True
        )

        resp = client.post("/api/system/slskd/reconnect")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}

    def test_failure(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.routes.system.health.reconnect_slskd", lambda config: False
        )

        resp = client.post("/api/system/slskd/reconnect")
        assert resp.status_code == 200
        assert resp.json() == {"success": False}


class TestListenBrainzCheck:
    def test_check_caches_result_for_next_status_poll(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.routes.system.health.check_listenbrainz",
            lambda config: ServiceHealth(
                name="listenbrainz", status="up", latency_ms=99
            ),
        )
        monkeypatch.setattr(
            "app.routes.system.health.check_all",
            lambda config, listenbrainz_cached=None: {
                "slskd": ServiceHealth(name="slskd", status="up", latency_ms=1),
                "navidrome": ServiceHealth(name="navidrome", status="up", latency_ms=1),
                "listenbrainz": listenbrainz_cached
                or ServiceHealth(name="listenbrainz", status="unknown"),
            },
        )

        # Before any check: status poll reports "unknown", not a live check.
        resp = client.get("/api/system/status")
        assert resp.json()["services"]["listenbrainz"]["status"] == "unknown"

        # On-demand check runs live and caches the result.
        resp = client.post("/api/system/listenbrainz/check")
        assert resp.status_code == 200
        assert resp.json() == {"status": "up", "latency_ms": 99, "error": None}

        # Next status poll reports the cached result, not "unknown" again.
        resp = client.get("/api/system/status")
        assert resp.json()["services"]["listenbrainz"]["status"] == "up"
        assert resp.json()["services"]["listenbrainz"]["latency_ms"] == 99


# ============================================================================
# POST /api/system/sync
# ============================================================================


class _FakeSyncWorker:
    def __init__(self, result: dict):
        self.result = result
        self.calls = 0

    def run_once(self) -> dict:
        self.calls += 1
        return self.result


class TestSyncNow:
    def test_runs_both_workers_and_returns_summaries(self) -> None:
        app = create_app()
        love_sync = _FakeSyncWorker({"synced": 2})
        trash_purge = _FakeSyncWorker({"trashed": 1})
        app.state.love_sync = love_sync
        app.state.trash_purge = trash_purge

        resp = TestClient(app).post("/api/system/sync")

        assert resp.status_code == 200
        assert resp.json() == {
            "love_sync": {"synced": 2},
            "trash_purge": {"trashed": 1},
        }
        assert love_sync.calls == 1
        assert trash_purge.calls == 1

    def test_returns_service_error_when_workers_are_unavailable(self, client) -> None:
        resp = client.post("/api/system/sync")

        assert resp.status_code == 503
        assert resp.json()["error"]["message"] == "Sync workers are not available"


# ============================================================================
# POST /api/system/consolidate
# ============================================================================


class TestConsolidate:
    def test_runs_the_sweep_and_returns_summary(self, client, monkeypatch) -> None:
        class _FakeBeets:
            def __init__(self, config):
                self.config = config

            def consolidate_all(self):
                return {"albums": 3, "moved": 2, "renamed": 6,
                        "deduplicated": 0, "errors": 0}

        monkeypatch.setattr("app.routes.system.BeetsService", _FakeBeets)
        resp = client.post("/api/system/consolidate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["moved"] == 2
        assert body["renamed"] == 6

    def test_disabled_beets_returns_skipped(self, client, monkeypatch) -> None:
        class _FakeBeets:
            def __init__(self, config):
                self.config = config

            def consolidate_all(self):
                return {"skipped": "beets.enabled is false"}

        monkeypatch.setattr("app.routes.system.BeetsService", _FakeBeets)
        resp = client.post("/api/system/consolidate")

        assert resp.status_code == 200
        assert resp.json() == {"skipped": "beets.enabled is false"}


# ============================================================================
# GET /api/logs
# ============================================================================


class TestGetLogs:
    def test_logs_contain_emitted_lines(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ring = RingBufferHandler(500)
        ring.setFormatter(logging.Formatter("%(message)s"))
        monkeypatch.setattr("app.logging_config._ring_buffer", ring)
        root = logging.getLogger()
        root.addHandler(ring)

        try:
            root.warning("test warning line")

            resp = client.get("/api/logs?limit=10")
            assert resp.status_code == 200
            text = resp.text
            assert "test warning line" in text
        finally:
            root.removeHandler(ring)

    def test_limit_zero_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/logs?limit=0")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_limit_over_max_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/logs?limit=1001")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_default_limit(self, client: TestClient) -> None:
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        # Response should be plain text
        assert resp.headers.get("content-type", "").startswith("text/plain")

    def test_response_ends_with_newline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ring = RingBufferHandler(500)
        ring.setFormatter(logging.Formatter("%(message)s"))
        monkeypatch.setattr("app.logging_config._ring_buffer", ring)
        root = logging.getLogger()
        root.addHandler(ring)

        try:
            root.warning("hello")

            resp = client.get("/api/logs?limit=1")
            assert resp.text.endswith("\n")
        finally:
            root.removeHandler(ring)


# ============================================================================
# POST /api/system/stop-slskd-activity
# ============================================================================


class FakeSearchServiceForStop(SearchService):
    """Minimal in-memory SearchService for stop-slskd-activity tests."""

    def __init__(self):
        self.jobs: dict[str, SearchJob] = {}
        self.cancel_calls: list[str] = []
        self.fail_cancel_for: set[str] = set()

    def add_job(self, search_id: str, status: str = "searching") -> None:
        self.jobs[search_id] = SearchJob(
            search_id=search_id,
            query="Test",
            artist=None,
            created_at=datetime.now(timezone.utc),
            status=status,
        )

    def search(self, query, artist=None) -> SearchJob:
        raise NotImplementedError

    def get_results(self, search_id: str) -> list[SearchResult]:
        raise NotImplementedError

    def cancel(self, search_id: str) -> bool:
        self.cancel_calls.append(search_id)
        if search_id in self.fail_cancel_for:
            return False
        self.jobs[search_id].status = "cancelled"
        return True

    def get_status(self, search_id: str) -> SearchJob:
        return self.jobs[search_id]

    def list_searches(self) -> list[SearchJob]:
        return list(self.jobs.values())

    def get_progress(self, search_id: str) -> dict:
        raise NotImplementedError


class FakeDownloadServiceForStop(DownloadService):
    """Minimal in-memory DownloadService for stop-slskd-activity tests."""

    def __init__(self):
        self.transfers: dict[str, Transfer] = {}
        self.cancel_calls: list[str] = []
        self.fail_cancel_for: set[str] = set()

    def add_transfer(self, transfer_id: str, state: str = "downloading") -> None:
        self.transfers[transfer_id] = Transfer(
            transfer_id=transfer_id,
            username="peer1",
            filename="song.mp3",
            size=1000,
            state=state,
            progress=0.0,
            speed=None,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
        )

    def queue(self, username, files, search_id=None, destination=None) -> QueueResult:
        raise NotImplementedError

    def get_status(self) -> list[Transfer]:
        return list(self.transfers.values())

    def retry(self, transfer_id: str) -> RetryResult:
        raise NotImplementedError

    def cancel(self, transfer_id: str) -> bool:
        self.cancel_calls.append(transfer_id)
        if transfer_id in self.fail_cancel_for:
            return False
        self.transfers[transfer_id].state = "cancelled"
        return True

    def delete_transfer(self, transfer_id: str) -> bool:
        raise NotImplementedError

    def get_transfer(self, transfer_id: str) -> Transfer:
        return self.transfers[transfer_id]


class TestStopSlskdActivity:
    def test_cancels_searching_and_active_transfers_only(self) -> None:
        search_service = FakeSearchServiceForStop()
        search_service.add_job("s1", status="searching")
        search_service.add_job("s2", status="completed")
        search_service.add_job("s3", status="searching")

        download_service = FakeDownloadServiceForStop()
        download_service.add_transfer("t1", state="downloading")
        download_service.add_transfer("t2", state="queued")
        download_service.add_transfer("t3", state="completed")

        app = create_app(
            search_service=search_service, download_service=download_service
        )
        client = TestClient(app)

        resp = client.post("/api/system/stop-slskd-activity")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "cancelled_searches": 2,
            "cancelled_transfers": 2,
            "failed_transfers": 0,
        }
        # Only the searching/active ones were touched — not the already
        # completed search or transfer.
        assert set(search_service.cancel_calls) == {"s1", "s3"}
        assert set(download_service.cancel_calls) == {"t1", "t2"}
        assert search_service.jobs["s2"].status == "completed"
        assert download_service.transfers["t3"].state == "completed"

    def test_counts_failed_transfer_cancels(self) -> None:
        search_service = FakeSearchServiceForStop()
        download_service = FakeDownloadServiceForStop()
        download_service.add_transfer("t1", state="downloading")
        download_service.fail_cancel_for.add("t1")

        app = create_app(
            search_service=search_service, download_service=download_service
        )
        client = TestClient(app)

        resp = client.post("/api/system/stop-slskd-activity")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled_transfers"] == 0
        assert body["failed_transfers"] == 1

    def test_nothing_active_returns_zeros(self) -> None:
        search_service = FakeSearchServiceForStop()
        download_service = FakeDownloadServiceForStop()

        app = create_app(
            search_service=search_service, download_service=download_service
        )
        client = TestClient(app)

        resp = client.post("/api/system/stop-slskd-activity")

        assert resp.status_code == 200
        assert resp.json() == {
            "cancelled_searches": 0,
            "cancelled_transfers": 0,
            "failed_transfers": 0,
        }
