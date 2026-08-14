"""
Integration tests for the setup wizard API routes (/api/setup/*).

Uses FastAPI TestClient with a real Config/Database backed by temp files.
Navidrome HTTP calls are mocked — these routes are exercised for their own
logic (status assembly, .env writes, error mapping), not live Navidrome
connectivity.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.db.database import Database
from app.main import create_app


def _write_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""\
[server]
port = 8000
host = "0.0.0.0"

[paths]
data_dir = "{tmp_path / "data"}"
music_dir = "/music"
download_dir = "downloads"
searches_dir = "Searches"
discovery_familiar_dir = "Discovery/Comfort_Zone"
discovery_new_releases_dir = "Discovery/Fresh_Picks"
discovery_exploration_dir = "Discovery/Deep_Cuts"

[navidrome]
url = "http://navidrome-server:4533"

[slskd]
url = "http://slskd:5030"

[listenbrainz]
enabled = false
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
comfort_zone_enabled = false
fresh_picks_enabled = false
deep_cuts_enabled = false
comfort_zone_interval_days = 1
deep_cuts_interval_days = 7
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
    (tmp_path / ".env").write_text("")


_SECRET_ENV_KEYS = [
    "NAVIDROME_USERNAME",
    "NAVIDROME_PASSWORD",
    "SLSKD_API_KEY",
    "SLSKD_NETWORK_USERNAME",
    "SLSKD_NETWORK_PASSWORD",
    "LISTENBRAINZ_TOKEN",
    "LISTENBRAINZ_USERNAME",
]


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch):
    """Config._load_env() sets os.environ globally, not scoped to one
    instance — a prior test's .env can leak a secret into this file's
    "nothing configured" assertions since os.environ persists across the
    whole pytest process. Force a clean slate regardless of run order.
    """
    for key in _SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _write_config(tmp_path)
    cfg = Config(
        config_path=str(tmp_path / "config.toml"),
        env_path=str(tmp_path / ".env"),
    )
    cfg.load()
    db = Database(cfg)
    db.initialize_schema()
    app = create_app(config=cfg, database=db)
    return TestClient(app)


@pytest.fixture
def config(client):
    return client.app.state.config


class TestSetupStatus:
    def test_nothing_configured(self, client):
        resp = client.get("/api/setup/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "wizard_completed": False,
            "tutorial_dismissed": False,
            "navidrome_configured": False,
            "slskd_configured": False,
            "listenbrainz_configured": False,
        }

    def test_reflects_saved_navidrome_creds(self, client, config):
        # Secrets require a restart to take effect in-memory (same as
        # /api/config/secrets) — set directly to simulate post-restart state.
        config.navidrome.username = "alice"
        config.navidrome.password = "secret"
        resp = client.get("/api/setup/status")
        assert resp.json()["navidrome_configured"] is True


class TestSetupNavidrome:
    @patch("app.routes.setup.requests.post")
    def test_creates_admin_on_fresh_navidrome(self, mock_post, client, config):
        mock_post.return_value = Mock(status_code=200)

        resp = client.post(
            "/api/setup/navidrome", json={"username": "alice", "password": "secret"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"created": True}
        assert config.env_path.read_text().count("NAVIDROME_USERNAME")

    @patch("app.routes.setup.requests.get")
    @patch("app.routes.setup.requests.post")
    def test_falls_back_to_verify_when_admin_exists(
        self, mock_post, mock_get, client, config
    ):
        mock_post.return_value = Mock(status_code=403)
        mock_get.return_value = Mock(
            json=lambda: {"subsonic-response": {"status": "ok"}}
        )

        resp = client.post(
            "/api/setup/navidrome", json={"username": "alice", "password": "secret"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"created": False}

    @patch("app.routes.setup.requests.get")
    @patch("app.routes.setup.requests.post")
    def test_wrong_credentials_against_existing_admin_returns_400(
        self, mock_post, mock_get, client
    ):
        mock_post.return_value = Mock(status_code=403)
        mock_get.return_value = Mock(
            json=lambda: {"subsonic-response": {"status": "failed"}}
        )

        resp = client.post(
            "/api/setup/navidrome", json={"username": "alice", "password": "wrong"}
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"

    @patch("app.routes.setup.requests.post")
    def test_navidrome_unreachable_returns_503(self, mock_post, client):
        import requests

        mock_post.side_effect = requests.ConnectionError("refused")

        resp = client.post(
            "/api/setup/navidrome", json={"username": "alice", "password": "secret"}
        )

        assert resp.status_code == 503


class TestSetupSlskd:
    def test_saves_credentials_and_flags_restart(self, client, config):
        resp = client.post(
            "/api/setup/slskd", json={"username": "bob", "password": "hunter2"}
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is True
        assert body["requires_restart"] is True
        env_text = config.env_path.read_text()
        assert 'SLSKD_NETWORK_USERNAME="bob"' in env_text
        assert 'SLSKD_NETWORK_PASSWORD="hunter2"' in env_text


class TestSlskdCheck:
    @patch("app.routes.setup.requests.get")
    def test_connected_reports_no_error(self, mock_get, client):
        mock_get.return_value = Mock(status_code=200, json=lambda: {"isConnected": True})

        resp = client.get("/api/setup/slskd/check")

        assert resp.status_code == 200
        assert resp.json() == {"connected": True, "error": None}

    @patch("app.routes.setup.get_slskd_login_error")
    @patch("app.routes.setup.requests.get")
    def test_disconnected_surfaces_login_error(self, mock_get, mock_reason, client):
        mock_get.return_value = Mock(status_code=200, json=lambda: {"isConnected": False})
        mock_reason.return_value = (
            "slskd rejected this Soulseek login — the username is already "
            "registered under a different password"
        )

        resp = client.get("/api/setup/slskd/check")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert "already registered" in body["error"]

    @patch("app.routes.setup.get_slskd_login_error")
    @patch("app.routes.setup.requests.get")
    def test_unreachable_slskd_reports_disconnected(self, mock_get, mock_reason, client):
        import requests

        mock_get.side_effect = requests.ConnectionError("refused")
        mock_reason.return_value = None

        resp = client.get("/api/setup/slskd/check")

        assert resp.status_code == 200
        assert resp.json()["connected"] is False


class TestWizardLifecycle:
    def test_complete_sets_flag(self, client):
        assert client.get("/api/setup/status").json()["wizard_completed"] is False
        assert client.post("/api/setup/complete").json() == {"ok": True}
        assert client.get("/api/setup/status").json()["wizard_completed"] is True

    def test_dismiss_tutorial_sets_flag(self, client):
        assert client.post("/api/setup/tutorial/dismiss").json() == {"ok": True}
        assert client.get("/api/setup/status").json()["tutorial_dismissed"] is True

    def test_rerun_clears_flags(self, client):
        client.post("/api/setup/complete")
        client.post("/api/setup/tutorial/dismiss")

        resp = client.post("/api/setup/rerun")

        assert resp.json() == {"ok": True}
        status = client.get("/api/setup/status").json()
        assert status["wizard_completed"] is False
        assert status["tutorial_dismissed"] is False

    def test_rerun_does_not_clear_saved_credentials(self, client, config):
        config.navidrome.username = "alice"
        config.navidrome.password = "secret"
        client.post("/api/setup/complete")

        client.post("/api/setup/rerun")

        assert client.get("/api/setup/status").json()["navidrome_configured"] is True
