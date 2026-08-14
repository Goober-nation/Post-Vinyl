"""
Integration tests for Recs API routes (P6-9).

Uses FastAPI TestClient with a real Config (backed by temp files), a real
in-memory-backed Database (initialize_schema), and a FakeRecPuller injected
manually onto app.state.rec_puller — RecPuller normally attaches to app.state
inside the app's lifespan, which does not run under a bare TestClient (no
`with TestClient(app):` context), so tests must set it after create_app().

Exercises the 4 endpoints plus the global error format:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.recs_store import RecsStore
from app.main import create_app
from app.services.interfaces.download import Transfer


def _write_config(
    tmp_path: Path,
    *,
    comfort_zone_enabled=True,
    fresh_picks_enabled=True,
    deep_cuts_enabled=True,
    lb_enabled=True,
    comfort_zone_interval_days=1,
    deep_cuts_interval_days=7,
):
    """Write a minimal but complete config.toml covering all 10 sections."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""\
[server]
port = 8000
host = "0.0.0.0"

[paths]
data_dir = "{tmp_path}/data"
music_dir = "/music"
download_dir = "/music/downloads"
searches_dir = "/music/searches"
discovery_familiar_dir = "Discovery/Comfort_Zone"
discovery_new_releases_dir = "Discovery/Fresh_Picks"
discovery_exploration_dir = "Discovery/Deep_Cuts"

[navidrome]
url = "http://navidrome-server:4533"

[slskd]
url = "http://slskd:5030"

[listenbrainz]
url = "https://api.listenbrainz.org"

[search]
wait_seconds = 10
poll_interval = 1
response_threshold = 10
min_wait_seconds = 3

[download]
check_interval = 15
max_retries_per_track = 3
bad_peer_threshold = 1
upload_limit_mb = 50

[recs]
comfort_zone_enabled = {"true" if comfort_zone_enabled else "false"}
fresh_picks_enabled = {"true" if fresh_picks_enabled else "false"}
deep_cuts_enabled = {"true" if deep_cuts_enabled else "false"}
comfort_zone_interval_days = {comfort_zone_interval_days}
deep_cuts_interval_days = {deep_cuts_interval_days}
comfort_zone_playlist_name = "Comfort Zone"
fresh_picks_playlist_name = "Fresh Picks"
deep_cuts_playlist_name = "Deep Cuts"
comfort_zone_count = 5
deep_cuts_count = 5

[fresh_picks]
pull_window = "30d"
offset = 50
count = 5
search_buffer = 25

[sync]
interval_hours = 12

[logging]
level = "INFO"
format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
"""
    )
    env_path = tmp_path / ".env"
    env_lines = ['SLSKD_API_KEY="real-key"']
    if lb_enabled:
        # enabled is now derived from username+token both being present.
        env_lines += ['LISTENBRAINZ_TOKEN="lb-token"', 'LISTENBRAINZ_USERNAME="lb-user"']
    env_path.write_text("\n".join(env_lines) + "\n")
    return config_path, env_path


class FakeRecPuller:
    """Stand-in for the real RecPuller worker (trigger_pull/is_running/last_run_at)."""

    def __init__(
        self,
        *,
        trigger_result=True,
        running=False,
        last_run_at=None,
        next_periodic_pull_at=None,
    ):
        self._trigger_result = trigger_result
        self._running = running
        self._last_run_at = last_run_at
        self._next_periodic_pull_at = next_periodic_pull_at
        self.trigger_calls = 0
        self.trigger_categories = None
        self.abort_calls = 0

    def trigger_pull(self, categories=None) -> bool:
        self.trigger_calls += 1
        self.trigger_categories = categories
        return self._trigger_result

    def is_running(self) -> bool:
        return self._running

    def last_run_at(self):
        return self._last_run_at

    def next_periodic_pull_at(self):
        return self._next_periodic_pull_at

    def request_abort(self) -> bool:
        self.abort_calls += 1
        return self._running


class FakeDownloadServiceForRecs:
    """Minimal DownloadService stand-in for cancel-queued/abort tests."""

    def __init__(self, live_transfers=None):
        self._live_transfers = live_transfers or []
        self.cancel_calls: list[str] = []

    def get_status(self):
        return self._live_transfers

    def cancel(self, transfer_id: str) -> bool:
        self.cancel_calls.append(transfer_id)
        return True


def _make_app(
    tmp_path,
    *,
    comfort_zone_enabled=True,
    fresh_picks_enabled=True,
    deep_cuts_enabled=True,
    lb_enabled=True,
    comfort_zone_interval_days=1,
    deep_cuts_interval_days=7,
    rec_puller=None,
    download_service=None,
):
    config_path, env_path = _write_config(
        tmp_path,
        comfort_zone_enabled=comfort_zone_enabled,
        fresh_picks_enabled=fresh_picks_enabled,
        deep_cuts_enabled=deep_cuts_enabled,
        lb_enabled=lb_enabled,
        comfort_zone_interval_days=comfort_zone_interval_days,
        deep_cuts_interval_days=deep_cuts_interval_days,
    )
    cfg = Config(config_path=str(config_path), env_path=str(env_path))
    cfg.load()

    class MockPaths:
        pass

    db_paths = MockPaths()
    db_paths.data_dir = str(tmp_path / "dbdata")

    class MockDbConfig:
        pass

    db_cfg = MockDbConfig()
    db_cfg.paths = db_paths
    db = Database(db_cfg)
    db.initialize_schema()

    app = create_app(config=cfg, database=db, download_service=download_service)
    app.state.rec_puller = rec_puller if rec_puller is not None else FakeRecPuller()
    return app, cfg, db


# ============================================================================
# GET /api/recs/status
# ============================================================================


class TestRecsStatus:
    def test_status_shape_no_pulls_yet(self, tmp_path):
        app, _cfg, _db = _make_app(
            tmp_path, comfort_zone_interval_days=2, deep_cuts_interval_days=9
        )
        client = TestClient(app)

        resp = client.get("/api/recs/status")
        assert resp.status_code == 200
        body = resp.json()

        assert body["comfort_zone_enabled"] is True
        assert body["fresh_picks_enabled"] is True
        assert body["deep_cuts_enabled"] is True
        assert body["listenbrainz_enabled"] is True
        assert body["comfort_zone_interval_days"] == 2
        assert body["deep_cuts_interval_days"] == 9
        assert body["comfort_zone_playlist_name"] == "Comfort Zone"
        assert body["fresh_picks_playlist_name"] == "Fresh Picks"
        assert body["deep_cuts_playlist_name"] == "Deep Cuts"
        assert body["rotation_trash_rating"] == 1
        assert body["counts"] == {
            "comfort_zone_count": 5,
            "deep_cuts_count": 5,
        }
        assert body["fresh_picks"] == {
            "pull_window": "30d",
            "offset": 50,
            "count": 5,
            "search_buffer": 25,
        }
        assert body["status_counts"] == {}
        assert body["last_pull_at"] is None
        assert body["next_pull_at"] is None
        assert body["running"] is False

    def test_status_counts_from_store(self, tmp_path):
        app, _cfg, db = _make_app(tmp_path)
        store = RecsStore(db)
        store.insert_rec("comfort_zone", "A1", "T1", None, "in_library")
        store.insert_rec("comfort_zone", "A2", "T2", None, "queued")
        store.insert_rec("fresh_picks", "A3", "T3", None, "queued")

        client = TestClient(app)
        resp = client.get("/api/recs/status")
        assert resp.status_code == 200
        assert resp.json()["status_counts"] == {"in_library": 1, "queued": 2}

    def test_last_and_next_pull_at_reflect_rec_puller(self, tmp_path):
        """P6.5-2: with per-category intervals, the route no longer computes
        next_pull_at itself — it's a straight passthrough of whatever
        RecPuller.next_periodic_pull_at() reports (that method owns the
        per-category due-math, tested directly in test_rec_puller.py)."""
        last_run = 1_700_000_000.0
        next_run = 1_700_100_000.0
        rec_puller = FakeRecPuller(last_run_at=last_run, next_periodic_pull_at=next_run)
        app, _cfg, _db = _make_app(tmp_path, rec_puller=rec_puller)
        client = TestClient(app)

        resp = client.get("/api/recs/status")
        body = resp.json()

        expected_last = datetime.fromtimestamp(last_run, tz=timezone.utc).isoformat()
        expected_next = datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat()
        assert body["last_pull_at"] == expected_last
        assert body["next_pull_at"] == expected_next

    def test_next_pull_at_none_when_rec_puller_reports_none(self, tmp_path):
        """P6.5-1: last_pull_at and next_pull_at are independent — a manual
        pull can set last_run_at while RecPuller still reports no periodic
        pull scheduled (e.g. no category enabled), and the route must
        surface that faithfully rather than deriving one from the other."""
        last_run = 1_700_000_000.0
        rec_puller = FakeRecPuller(last_run_at=last_run, next_periodic_pull_at=None)
        app, _cfg, _db = _make_app(
            tmp_path,
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=False,
            rec_puller=rec_puller,
        )
        client = TestClient(app)

        resp = client.get("/api/recs/status")
        body = resp.json()

        expected_last = datetime.fromtimestamp(last_run, tz=timezone.utc).isoformat()
        assert body["comfort_zone_enabled"] is False
        assert body["last_pull_at"] == expected_last
        assert body["next_pull_at"] is None

    def test_running_flag_reflects_rec_puller(self, tmp_path):
        rec_puller = FakeRecPuller(running=True)
        app, _cfg, _db = _make_app(tmp_path, rec_puller=rec_puller)
        client = TestClient(app)

        resp = client.get("/api/recs/status")
        assert resp.json()["running"] is True


# ============================================================================
# POST /api/recs/pull
# ============================================================================


class TestPullRecs:
    def test_pull_started_202(self, tmp_path):
        rec_puller = FakeRecPuller(trigger_result=True)
        app, _cfg, _db = _make_app(tmp_path, rec_puller=rec_puller)
        client = TestClient(app)

        resp = client.post("/api/recs/pull")
        assert resp.status_code == 202
        assert resp.json() == {"started": True}
        assert rec_puller.trigger_calls == 1

    def test_pull_conflict_409(self, tmp_path):
        rec_puller = FakeRecPuller(trigger_result=False)
        app, _cfg, _db = _make_app(tmp_path, rec_puller=rec_puller)
        client = TestClient(app)

        resp = client.post("/api/recs/pull")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "REC_PULL_IN_PROGRESS"
        assert rec_puller.trigger_calls == 1

    def test_pull_selected_categories(self, tmp_path):
        rec_puller = FakeRecPuller(trigger_result=True)
        app, _cfg, _db = _make_app(tmp_path, rec_puller=rec_puller)
        client = TestClient(app)

        resp = client.post("/api/recs/pull", json={"categories": ["fresh_picks"]})

        assert resp.status_code == 202
        assert rec_puller.trigger_categories == ["fresh_picks"]

    def test_pull_empty_categories_rejected(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/recs/pull", json={"categories": []})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ============================================================================
# POST /api/recs/settings
# ============================================================================


class TestUpdateRecsSettings:
    def test_updates_and_hot_reloads(self, tmp_path):
        app, cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/recs/settings",
            json={"recs": {"comfort_zone_interval_days": 6, "comfort_zone_count": 2}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["requires_restart"] == []
        assert body["config"]["recs"]["comfort_zone_interval_days"] == 6
        assert body["config"]["recs"]["comfort_zone_count"] == 2

        # In-memory config hot-applied (recs is a HOT_SECTIONS member)
        assert cfg.recs.comfort_zone_interval_days == 6
        assert cfg.recs.comfort_zone_count == 2

        # Persisted to config.toml
        assert "comfort_zone_interval_days = 6" in cfg.config_path.read_text()

    def test_fresh_picks_section_editable_directly(self, tmp_path):
        """2026-08-13: the recs.fresh_picks_count alias is gone — the Recs
        tab edits the canonical [fresh_picks] section through the same
        endpoint."""
        app, cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/recs/settings",
            json={"fresh_picks": {"count": 9, "offset": 30}},
        )
        assert resp.status_code == 200
        assert resp.json()["requires_restart"] == []
        assert cfg.fresh_picks.count == 9
        assert cfg.fresh_picks.offset == 30
        assert "count = 9" in cfg.config_path.read_text()

    def test_empty_body_no_op(self, tmp_path):
        app, cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/recs/settings", json={})
        assert resp.status_code == 200
        assert resp.json()["requires_restart"] == []

    def test_invalid_field_rejected(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/recs/settings", json={"bogus_field": 1})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_value_rejected(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post(
            "/api/recs/settings", json={"recs": {"comfort_zone_count": -1}}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ============================================================================
# GET /api/recs/pending
# ============================================================================


class TestGetPendingRecs:
    def test_pending_all(self, tmp_path):
        app, _cfg, db = _make_app(tmp_path)
        store = RecsStore(db)
        store.insert_rec("comfort_zone", "A1", "T1", None, "queued")
        store.insert_rec("comfort_zone", "A2", "T2", None, "in_library")

        client = TestClient(app)
        resp = client.get("/api/recs/pending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_pending_filtered_by_status(self, tmp_path):
        app, _cfg, db = _make_app(tmp_path)
        store = RecsStore(db)
        store.insert_rec("comfort_zone", "A1", "T1", None, "queued")
        store.insert_rec("comfort_zone", "A2", "T2", None, "queued")
        store.insert_rec("fresh_picks", "A3", "T3", None, "downloaded")

        client = TestClient(app)
        resp = client.get("/api/recs/pending", params={"status": "queued"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert all(item["status"] == "queued" for item in body["items"])

    def test_pending_invalid_status_400(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/recs/pending", params={"status": "pending"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_pending_limit_out_of_bounds_400(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.get("/api/recs/pending", params={"limit": 0})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_pending_limit_and_offset(self, tmp_path):
        app, _cfg, db = _make_app(tmp_path)
        store = RecsStore(db)
        for i in range(5):
            store.insert_rec("comfort_zone", f"A{i}", f"T{i}", None, "queued")

        client = TestClient(app)
        resp = client.get("/api/recs/pending", params={"limit": 2, "offset": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

    def test_pending_default_page_is_40_rows(self, tmp_path):
        app, _cfg, db = _make_app(tmp_path)
        store = RecsStore(db)
        for i in range(45):
            store.insert_rec("comfort_zone", f"A{i}", f"T{i}", None, "queued")

        client = TestClient(app)
        resp = client.get("/api/recs/pending")

        assert resp.status_code == 200
        assert resp.json()["total"] == 45
        assert len(resp.json()["items"]) == 40


# ============================================================================
# POST /api/recs/pending/cancel-queued
# ============================================================================


class TestCancelQueuedRecs:
    def test_cancels_queued_recs_and_live_transfer(self, tmp_path):
        app, _cfg, db = _make_app(
            tmp_path,
            download_service=FakeDownloadServiceForRecs(
                live_transfers=[
                    Transfer(
                        transfer_id="t1",
                        username="peer1",
                        filename="song.mp3",
                        size=1000,
                        state="downloading",
                        progress=0.0,
                        speed=None,
                        started_at=datetime.now(timezone.utc),
                        completed_at=None,
                        is_rec_download=True,
                    )
                ]
            ),
        )
        rec_store = RecsStore(db)
        rec_store.insert_rec("comfort_zone", "A1", "T1", None, "queued", search_id="s1")
        DownloadStore(db).insert_pending("s1", "peer1", "song.mp3", 1000, True)

        client = TestClient(app)
        resp = client.post("/api/recs/pending/cancel-queued")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "cancelled_recs": 1,
            "cancelled_transfers": 1,
            "failed_transfers": 0,
        }
        assert rec_store.count_recs_by_status() == {"cancelled": 1}

    def test_no_queued_recs_is_a_no_op(self, tmp_path):
        app, _cfg, _db = _make_app(tmp_path, download_service=FakeDownloadServiceForRecs())
        client = TestClient(app)

        resp = client.post("/api/recs/pending/cancel-queued")

        assert resp.status_code == 200
        assert resp.json() == {
            "cancelled_recs": 0,
            "cancelled_transfers": 0,
            "failed_transfers": 0,
        }


# ============================================================================
# POST /api/recs/abort
# ============================================================================


class TestAbortRecs:
    def test_aborts_running_pull_and_cancels_queued(self, tmp_path):
        rec_puller = FakeRecPuller(running=True)
        app, _cfg, db = _make_app(
            tmp_path,
            rec_puller=rec_puller,
            download_service=FakeDownloadServiceForRecs(),
        )
        rec_store = RecsStore(db)
        rec_store.insert_rec("comfort_zone", "A1", "T1", None, "queued", search_id="s1")

        client = TestClient(app)
        resp = client.post("/api/recs/abort")

        assert resp.status_code == 200
        body = resp.json()
        assert body["aborted_pull"] is True
        assert body["cancelled_recs"] == 1
        assert rec_puller.abort_calls == 1

    def test_abort_when_no_pull_running(self, tmp_path):
        rec_puller = FakeRecPuller(running=False)
        app, _cfg, _db = _make_app(
            tmp_path,
            rec_puller=rec_puller,
            download_service=FakeDownloadServiceForRecs(),
        )

        client = TestClient(app)
        resp = client.post("/api/recs/abort")

        assert resp.status_code == 200
        body = resp.json()
        assert body["aborted_pull"] is False
        assert body["cancelled_recs"] == 0
        assert rec_puller.abort_calls == 1
