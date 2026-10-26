"""
Integration tests for Config API routes (P4-4).

Uses FastAPI TestClient with a real Config instance backed by temporary files.
Exercises the 3 endpoints plus the global error format:
    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.exceptions import ConfigValidationError
from app.main import create_app


def _write_config(tmp_path: Path) -> None:
    """Write a minimal but complete config.toml covering all 10 sections."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """\
[server]
port = 8000
host = "0.0.0.0"

[paths]
data_dir = "/app/data"
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

    env_path = tmp_path / ".env"
    env_path.write_text('SLSKD_API_KEY="real-key"\nNAVIDROME_PASSWORD="real-pass"\n')


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a TestClient with a config backed by temp files."""
    _write_config(tmp_path)
    cfg = Config(
        config_path=str(tmp_path / "config.toml"),
        env_path=str(tmp_path / ".env"),
    )
    cfg.load()
    app = create_app(config=cfg)
    return TestClient(app)


@pytest.fixture
def service(client):
    """Return the Config instance from app state."""
    return client.app.state.config


# ============================================================================
# GET /api/config
# ============================================================================


class TestGetConfig:
    def test_returns_200_with_full_config(self, client):
        resp = client.get("/api/config")

        assert resp.status_code == 200
        body = resp.json()
        assert "server" in body
        assert "paths" in body
        assert "navidrome" in body
        assert "slskd" in body
        assert "listenbrainz" in body
        assert "search" in body
        assert "download" in body
        assert "recs" in body
        assert "fresh_picks" in body
        assert "sync" in body
        assert "logging" in body

    def test_category_paths_are_resolved_and_base_discovery_is_not_exposed(self, client):
        paths = client.get("/api/config").json()["paths"]

        assert paths["discovery_familiar_path"] == "/music/Discovery/Comfort_Zone"
        assert paths["discovery_new_releases_path"] == "/music/Discovery/Fresh_Picks"
        assert paths["discovery_exploration_path"] == "/music/Discovery/Deep_Cuts"
        assert "discovery_dir" not in paths

    def test_base_discovery_setting_is_not_editable(self, client):
        response = client.post("/api/config", json={"paths": {"discovery_dir": "Other"}})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_secrets_are_masked(self, client) -> None:
        """Secrets appear as '***' in the response."""
        resp = client.get("/api/config")

        assert resp.status_code == 200
        body = resp.json()
        assert body["navidrome"]["password"] == "***"
        assert body["slskd"]["api_key"] == "***"

    def test_non_secret_value_matches(self, client) -> None:
        """A non-secret value matches the loaded config."""
        resp = client.get("/api/config")

        assert resp.status_code == 200
        body = resp.json()
        assert body["search"]["wait_seconds"] == 10

    def test_fresh_picks_settings_hot_reload(self, client, service):
        resp = client.post(
            "/api/config",
            json={
                "fresh_picks": {
                    "pull_window": "30d",
                    "offset": 25,
                    "count": 7,
                    "search_buffer": 3,
                }
            },
        )

        assert resp.status_code == 200
        assert resp.json()["requires_restart"] == []
        assert service.fresh_picks.offset == 25
        assert service.fresh_picks.count == 7
        assert resp.json()["config"]["fresh_picks"]["search_buffer"] == 3

    def test_get_config_picks_up_external_hot_reload(self, client, service):
        """Refreshing the frontend reflects a manual edit to a hot section."""
        content = service.config_path.read_text().replace("offset = 50", "offset = 150")
        service.config_path.write_text(content)

        response = client.get("/api/config")

        assert response.status_code == 200
        assert response.json()["fresh_picks"]["offset"] == 150
        assert service.fresh_picks.offset == 150


# ============================================================================
# POST /api/config
# ============================================================================


class TestUpdateConfig:
    def test_hot_section_updates_in_memory(self, client, service: Config) -> None:
        """A hot section (search) updates in-memory config and file."""
        resp = client.post("/api/config", json={"search": {"wait_seconds": 5}})

        assert resp.status_code == 200
        body = resp.json()
        assert body["requires_restart"] == []
        assert service.search.wait_seconds == 5
        assert "wait_seconds = 5" in service.config_path.read_text()

    def test_restart_section_updates_file_only(self, client, service: Config) -> None:
        """A restart section (paths) updates file but not in-memory config."""
        resp = client.post("/api/config", json={"paths": {"data_dir": "/new/data"}})

        assert resp.status_code == 200
        body = resp.json()
        assert body["requires_restart"] == ["paths"]
        assert str(service.paths.data_dir) == "/app/data"
        assert 'data_dir = "/new/data"' in service.config_path.read_text()

    def test_empty_body_returns_current_config(self, client) -> None:
        """Empty body returns current config with empty requires_restart."""
        resp = client.post("/api/config", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["requires_restart"] == []
        assert body["config"]["search"]["wait_seconds"] == 10

    def test_invalid_wait_seconds_zero_returns_400(self, client) -> None:
        """wait_seconds=0 fails pydantic validation (ge=1)."""
        resp = client.post("/api/config", json={"search": {"wait_seconds": 0}})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_logging_level_returns_400(self, client) -> None:
        """Invalid logging level fails field validation."""
        resp = client.post("/api/config", json={"logging": {"level": "LOUD"}})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_section_key_returns_400(self, client) -> None:
        """Unknown field within a section fails extra=forbid."""
        resp = client.post("/api/config", json={"paths": {"data_dirx": "x"}})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_top_level_section_returns_400(self, client) -> None:
        """Unknown top-level key fails extra=forbid on ConfigUpdateRequest."""
        resp = client.post("/api/config", json={"foo": {"bar": 1}})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_rollback_on_validation_error(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigValidationError during reload restores original config.toml content."""
        cfg = client.app.state.config
        original_bytes = cfg.config_path.read_bytes()

        call_count = 0
        original_reload = cfg.reload

        def raiser() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConfigValidationError("search.wait_seconds", 0, "test rollback")
            return original_reload()

        monkeypatch.setattr(cfg, "reload", raiser)

        resp = client.post("/api/config", json={"search": {"wait_seconds": 5}})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"
        assert cfg.config_path.read_bytes() == original_bytes


# ============================================================================
# POST /api/config/secrets
# ============================================================================


class TestUpdateSecrets:
    def test_update_secrets_writes_env_and_returns_200(
        self, client, service: Config
    ) -> None:
        """Updating secrets writes to .env and returns updated keys."""
        resp = client.post(
            "/api/config/secrets",
            json={"slskd_api_key": "new-key", "navidrome_password": "new-pass"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] == ["NAVIDROME_PASSWORD", "SLSKD_API_KEY"]
        assert body["requires_restart"] is True

        env_content = service.env_path.read_text()
        assert 'SLSKD_API_KEY="new-key"' in env_content
        assert 'NAVIDROME_PASSWORD="new-pass"' in env_content

    def test_secrets_do_not_change_in_memory_config(
        self, client, service: Config
    ) -> None:
        """In-memory config retains values from load() time after secrets update."""
        client.post(
            "/api/config/secrets",
            json={"slskd_api_key": "new-key", "navidrome_password": "new-pass"},
        )

        assert service.slskd.api_key == "real-key"
        assert service.navidrome.password == "real-pass"

    def test_empty_value_returns_400(self, client) -> None:
        """Empty secret value fails validation."""
        resp = client.post("/api/config/secrets", json={"slskd_api_key": ""})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_key_returns_400(self, client) -> None:
        """Unknown key fails extra=forbid."""
        resp = client.post("/api/config/secrets", json={"slskd_token": "x"})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_quote_in_value_returns_400(self, client) -> None:
        """Double quote in value fails validation."""
        resp = client.post("/api/config/secrets", json={"slskd_api_key": 'a"b'})

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_body_returns_empty_updated(self, client) -> None:
        """Empty body returns empty updated and requires_restart=True."""
        resp = client.post("/api/config/secrets", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] == []
        assert body["requires_restart"] is True
