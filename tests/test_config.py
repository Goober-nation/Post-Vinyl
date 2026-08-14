"""
Unit tests for configuration system.
"""

import os
import tempfile
from pathlib import Path

import pytest

from app.config import Config
from app.exceptions import ConfigError, ConfigNotFoundError, ConfigValidationError


class TestConfigLoading:
    """Test config loading from TOML and .env."""

    def test_load_minimal_config(self):
        """Config should load with minimal TOML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.server.port == 8000
            assert config.server.host == "0.0.0.0"  # default

    def test_load_full_config(self):
        """Config should load all sections from TOML."""
        for key in ["LISTENBRAINZ_TOKEN", "LISTENBRAINZ_USERNAME"]:
            os.environ.pop(key, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("""
[server]
port = 9000
host = "127.0.0.1"

[paths]
data_dir = "/custom/data"
music_dir = "/custom/music"

[navidrome]
url = "http://custom:4533"

[slskd]
url = "http://custom:5030"

[listenbrainz]
url = "https://custom.api"

[search]
wait_seconds = 15
poll_interval = 2

[download]
check_interval = 20
max_retries_per_track = 5

[recs]
comfort_zone_enabled = true
fresh_picks_enabled = true
deep_cuts_enabled = true
comfort_zone_interval_days = 2
deep_cuts_interval_days = 14
comfort_zone_playlist_name = "Comfort Zone"
fresh_picks_playlist_name = "Fresh Picks"
deep_cuts_playlist_name = "Deep Cuts"

[sync]
interval_hours = 24

[logging]
level = "DEBUG"
""")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.server.port == 9000
            assert config.server.host == "127.0.0.1"
            assert str(config.paths.data_dir) == "/custom/data"
            assert config.navidrome.url == "http://custom:4533"
            assert config.slskd.url == "http://custom:5030"
            assert config.listenbrainz.url == "https://custom.api"
            assert config.listenbrainz.enabled is False  # no token/username set
            assert config.search.wait_seconds == 15
            assert config.download.check_interval == 20
            assert config.recs.comfort_zone_enabled is True
            assert config.recs.fresh_picks_enabled is True
            assert config.recs.deep_cuts_enabled is True
            assert config.recs.comfort_zone_interval_days == 2
            assert config.recs.deep_cuts_interval_days == 14
            assert config.recs.comfort_zone_playlist_name == "Comfort Zone"
            assert config.recs.fresh_picks_playlist_name == "Fresh Picks"
            assert config.recs.deep_cuts_playlist_name == "Deep Cuts"
            assert config.sync.interval_hours == 24
            assert config.logging.level == "DEBUG"

    def test_load_with_env_secrets(self):
        """Config should load secrets from .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            env_path = Path(tmpdir) / ".env"
            env_path.write_text("""
NAVIDROME_USERNAME=testuser
NAVIDROME_PASSWORD=testpass
SLSKD_API_KEY=testkey
LISTENBRAINZ_TOKEN=testtoken
LISTENBRAINZ_USERNAME=testlb
""")

            config = Config(config_path=str(config_path), env_path=str(env_path))
            config.load()

            assert config.navidrome.username == "testuser"
            assert config.navidrome.password == "testpass"
            assert config.slskd.api_key == "testkey"
            assert config.listenbrainz.token == "testtoken"
            assert config.listenbrainz.username == "testlb"

    def test_config_not_found(self):
        """Config should raise ConfigNotFoundError if file missing."""
        config = Config(config_path="/nonexistent/config.toml")

        with pytest.raises(ConfigNotFoundError):
            config.load()

    def test_env_file_optional(self):
        """Config should work without .env file."""
        # Clear any env vars from previous tests
        for key in [
            "NAVIDROME_USERNAME",
            "NAVIDROME_PASSWORD",
            "SLSKD_API_KEY",
            "LISTENBRAINZ_TOKEN",
            "LISTENBRAINZ_USERNAME",
        ]:
            os.environ.pop(key, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            config = Config(config_path=str(config_path), env_path="/nonexistent/.env")
            config.load()  # Should not raise

            assert config.navidrome.username == ""  # empty default


class TestConfigHotReload:
    """Test hot-reload functionality."""

    def test_reload_non_secret_settings(self):
        """reload() should update non-secret settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nwait_seconds = 10\n")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.search.wait_seconds == 10

            # Update TOML
            config_path.write_text("[search]\nwait_seconds = 20\n")

            # Reload
            config.reload()

            assert config.search.wait_seconds == 20

    def test_reload_if_changed_detects_external_edit(self):
        """External TOML edits are picked up by the next config-dependent request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[fresh_picks]\noffset = 50\n")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.reload_if_changed() is False
            assert config.fresh_picks.offset == 50

            config_path.write_text("[fresh_picks]\noffset = 150\n")

            assert config.reload_if_changed() is True
            assert config.fresh_picks.offset == 150
            assert config.reload_if_changed() is False

    def test_reload_preserves_secrets(self):
        """reload() should not change secrets from .env."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            env_path = Path(tmpdir) / ".env"
            env_path.write_text("NAVIDROME_USERNAME=original\n")

            config = Config(config_path=str(config_path), env_path=str(env_path))
            config.load()

            assert config.navidrome.username == "original"

            # Update .env (should be ignored on reload)
            env_path.write_text("NAVIDROME_USERNAME=changed\n")

            # Reload
            config.reload()

            # Secret should remain unchanged
            assert config.navidrome.username == "original"

    def test_reload_before_load_raises_error(self):
        """reload() should raise error if load() not called first."""
        config = Config()

        with pytest.raises(ConfigError, match="not loaded"):
            config.reload()


class TestConfigValidation:
    """Test configuration validation."""

    def test_validate_server_port_valid(self):
        """Valid port should pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8080\n")

            config = Config(config_path=str(config_path))
            config.load()  # Should not raise

            assert config.server.port == 8080

    def test_validate_server_port_invalid_low(self):
        """Port < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="server.port"):
                config.load()

    def test_validate_server_port_invalid_high(self):
        """Port > 65535 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 70000\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="server.port"):
                config.load()

    def test_validate_search_wait_seconds(self):
        """search.wait_seconds < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nwait_seconds = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="search.wait_seconds"):
                config.load()

    def test_validate_search_response_limit(self):
        """search.response_limit < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nresponse_limit = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="search.response_limit"):
                config.load()

    def test_validate_search_response_cap(self):
        """search.response_cap < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nresponse_cap = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="search.response_cap"):
                config.load()

    def test_validate_search_rate_limit_max_searches(self):
        """search.rate_limit_max_searches < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nrate_limit_max_searches = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="search.rate_limit_max_searches"
            ):
                config.load()

    def test_validate_search_rate_limit_window_seconds(self):
        """search.rate_limit_window_seconds < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nrate_limit_window_seconds = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="search.rate_limit_window_seconds"
            ):
                config.load()

    def test_validate_search_rate_limit_wait_timeout_seconds(self):
        """search.rate_limit_wait_timeout_seconds < 0 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nrate_limit_wait_timeout_seconds = -1\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError,
                match="search.rate_limit_wait_timeout_seconds",
            ):
                config.load()

    def test_validate_search_query_cache_ttl_seconds(self):
        """search.query_cache_ttl_seconds < 0 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nquery_cache_ttl_seconds = -1\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="search.query_cache_ttl_seconds"
            ):
                config.load()

    def test_validate_search_artist_match_min_words(self):
        """search.artist_match_min_words < 1 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nartist_match_min_words = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="search.artist_match_min_words"
            ):
                config.load()

    def test_search_artist_match_min_words_default_and_override(self):
        """Default is 1; an explicit value is honoured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")
            config = Config(config_path=str(config_path))
            config.load()
            assert config.search.artist_match_min_words == 1

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[search]\nartist_match_min_words = 2\n")
            config = Config(config_path=str(config_path))
            config.load()
            assert config.search.artist_match_min_words == 2

    def test_search_rate_limit_and_cache_defaults(self):
        """Defaults apply when the keys are absent from config.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.search.response_limit == 60
            assert config.search.response_cap == 250
            assert config.search.rate_limit_max_searches == 4
            assert config.search.rate_limit_window_seconds == 60
            assert config.search.rate_limit_wait_timeout_seconds == 45
            assert config.search.query_cache_ttl_seconds == 600

    def test_search_rate_limit_and_cache_overrides(self):
        """Explicit values in config.toml override the defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("""
[search]
response_limit = 75
response_cap = 500
rate_limit_max_searches = 8
rate_limit_window_seconds = 120
rate_limit_wait_timeout_seconds = 30
query_cache_ttl_seconds = 300
""")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.search.response_limit == 75
            assert config.search.response_cap == 500
            assert config.search.rate_limit_max_searches == 8
            assert config.search.rate_limit_window_seconds == 120
            assert config.search.rate_limit_wait_timeout_seconds == 30
            assert config.search.query_cache_ttl_seconds == 300

    def test_validate_download_max_retries(self):
        """download.max_retries_per_track < 0 should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[download]\nmax_retries_per_track = -1\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="download.max_retries_per_track"
            ):
                config.load()

    def test_validate_download_bad_peer_threshold(self):
        """download.bad_peer_threshold < 1 should fail validation (0 would
        blacklist a peer on its first failure)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[download]\nbad_peer_threshold = 0\n")

            config = Config(config_path=str(config_path))

            with pytest.raises(
                ConfigValidationError, match="download.bad_peer_threshold"
            ):
                config.load()

    def test_validate_logging_level_valid(self):
        """Valid logging level should pass validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "DEBUG"\n')

            config = Config(config_path=str(config_path))
            config.load()  # Should not raise

            assert config.logging.level == "DEBUG"

    def test_validate_logging_level_invalid(self):
        """Invalid logging level should fail validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[logging]\nlevel = "INVALID"\n')

            config = Config(config_path=str(config_path))

            with pytest.raises(ConfigValidationError, match="logging.level"):
                config.load()


class TestConfigSections:
    """Test individual config sections."""

    def test_server_config_defaults(self):
        """ServerConfig should have correct defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("")  # Empty

            config = Config(config_path=str(config_path))
            config.load()

            assert config.server.port == 8000
            assert config.server.host == "0.0.0.0"

    def test_paths_config(self):
        """PathsConfig should handle Path objects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text('[paths]\ndata_dir = "/test/data"\n')

            config = Config(config_path=str(config_path))
            config.load()

            assert isinstance(config.paths.data_dir, Path)
            assert str(config.paths.data_dir) == "/test/data"

    def test_per_category_discovery_dirs(self):
        """P6.7-0b: the three per-category discovery dirs default to
        subdirectories of the Discovery tree, are configurable, and resolve
        through their *_path properties."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text(
                '[paths]\nmusic_dir = "/music"\n'
                'discovery_familiar_dir = "Discovery/Comfort_Zone"\n'
                'discovery_new_releases_dir = "Discovery/Fresh_Picks"\n'
                'discovery_exploration_dir = "Discovery/Deep_Cuts"\n'
            )

            config = Config(config_path=str(config_path))
            config.load()

            assert config.paths.discovery_familiar_dir == "Discovery/Comfort_Zone"
            assert config.paths.discovery_new_releases_dir == "Discovery/Fresh_Picks"
            assert config.paths.discovery_exploration_dir == "Discovery/Deep_Cuts"
            assert config.paths.discovery_familiar_path == Path(
                "/music/Discovery/Comfort_Zone"
            )
            assert config.paths.discovery_new_releases_path == Path(
                "/music/Discovery/Fresh_Picks"
            )
            assert config.paths.discovery_exploration_path == Path(
                "/music/Discovery/Deep_Cuts"
            )
            # The paths section is exposed by to_dict() so GET /api/config
            # reports them truthfully even though the UI doesn't edit them.
            exported = config.to_dict()["paths"]
            assert exported["discovery_familiar_dir"] == "Discovery/Comfort_Zone"
            assert exported["discovery_new_releases_dir"] == "Discovery/Fresh_Picks"
            assert exported["discovery_exploration_dir"] == "Discovery/Deep_Cuts"
            assert exported["discovery_familiar_path"] == "/music/Discovery/Comfort_Zone"
            assert exported["discovery_new_releases_path"] == "/music/Discovery/Fresh_Picks"
            assert exported["discovery_exploration_path"] == "/music/Discovery/Deep_Cuts"
            assert "discovery_dir" not in exported

    def test_listenbrainz_config(self):
        """ListenBrainzConfig.enabled should be derived from username+token, not a TOML flag."""
        for key in ["LISTENBRAINZ_TOKEN", "LISTENBRAINZ_USERNAME"]:
            os.environ.pop(key, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[listenbrainz]\n")

            # No secrets set: disabled.
            config = Config(config_path=str(config_path))
            config.load()
            assert config.listenbrainz.enabled is False

            # Both username and token set via .env: enabled.
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("LISTENBRAINZ_TOKEN=tok\nLISTENBRAINZ_USERNAME=user\n")
            config = Config(config_path=str(config_path), env_path=str(env_path))
            config.load()
            assert config.listenbrainz.enabled is True

            # Only one of the two set: still disabled.
            os.environ.pop("LISTENBRAINZ_USERNAME", None)
            env_path.write_text("LISTENBRAINZ_TOKEN=tok\n")
            config = Config(config_path=str(config_path), env_path=str(env_path))
            config.load()
            assert config.listenbrainz.enabled is False

    def test_recs_config(self):
        """RecsConfig should handle all fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("""
[recs]
comfort_zone_enabled = true
fresh_picks_enabled = false
deep_cuts_enabled = true
comfort_zone_interval_days = 3
deep_cuts_interval_days = 10
comfort_zone_playlist_name = "My Comfort Zone"
fresh_picks_playlist_name = "My Fresh Picks"
deep_cuts_playlist_name = "My Deep Cuts"
comfort_zone_count = 10
deep_cuts_count = 6

[fresh_picks]
pull_window = "2d"
offset = 12
count = 8
search_buffer = 4
""")

            config = Config(config_path=str(config_path))
            config.load()

            assert config.recs.comfort_zone_enabled is True
            assert config.recs.fresh_picks_enabled is False
            assert config.recs.deep_cuts_enabled is True
            assert config.recs.comfort_zone_interval_days == 3
            assert config.recs.deep_cuts_interval_days == 10
            assert config.recs.comfort_zone_playlist_name == "My Comfort Zone"
            assert config.recs.fresh_picks_playlist_name == "My Fresh Picks"
            assert config.recs.deep_cuts_playlist_name == "My Deep Cuts"
            assert config.recs.comfort_zone_count == 10
            assert config.recs.deep_cuts_count == 6
            assert config.fresh_picks.pull_window == "2d"
            assert config.fresh_picks.offset == 12
            assert config.fresh_picks.count == 8
            assert config.fresh_picks.search_buffer == 4


class TestConfigToDict:
    """Test to_dict() method."""

    def test_to_dict_masks_secrets(self):
        """to_dict() should mask secret values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            env_path = Path(tmpdir) / ".env"
            env_path.write_text("NAVIDROME_USERNAME=user\nNAVIDROME_PASSWORD=pass\n")

            config = Config(config_path=str(config_path), env_path=str(env_path))
            config.load()

            result = config.to_dict()

            assert result["navidrome"]["username"] == "***"
            assert result["navidrome"]["password"] == "***"
            assert result["server"]["port"] == 8000

    def test_to_dict_includes_all_sections(self):
        """to_dict() should include all config sections."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            config_path.write_text("[server]\nport = 8000\n")

            config = Config(config_path=str(config_path))
            config.load()

            result = config.to_dict()

            assert "server" in result
            assert "paths" in result
            assert "navidrome" in result
            assert "slskd" in result
            assert "listenbrainz" in result
            assert "search" in result
            assert "download" in result
            assert "recs" in result
            assert "sync" in result
            assert "logging" in result


class TestConfigHelpers:
    """Test helper methods."""

    def test_get_str(self):
        """_get_str should handle missing keys."""
        config = Config()

        result = config._get_str({}, "key", "default")
        assert result == "default"

        result = config._get_str({"key": "value"}, "key", "default")
        assert result == "value"

    def test_get_int(self):
        """_get_int should handle type conversion."""
        config = Config()

        result = config._get_int({}, "key", 10)
        assert result == 10

        result = config._get_int({"key": 20}, "key", 10)
        assert result == 20

        result = config._get_int({"key": "30"}, "key", 10)
        assert result == 30

        result = config._get_int({"key": "invalid"}, "key", 10)
        assert result == 10  # fallback to default

    def test_get_bool(self):
        """_get_bool should handle various boolean formats."""
        config = Config()

        # Boolean
        assert config._get_bool({"key": True}, "key", False) is True
        assert config._get_bool({"key": False}, "key", True) is False

        # String
        assert config._get_bool({"key": "true"}, "key", False) is True
        assert config._get_bool({"key": "yes"}, "key", False) is True
        assert config._get_bool({"key": "1"}, "key", False) is True
        assert config._get_bool({"key": "false"}, "key", True) is False
        assert config._get_bool({"key": "no"}, "key", True) is False

        # Missing
        assert config._get_bool({}, "key", True) is True
        assert config._get_bool({}, "key", False) is False


class TestConfigSurfaceIsComplete:
    """A new config key has to be wired through five places, not four:
    `_populate_*`, `_validate`, `to_dict()`, the route's pydantic model, and
    the UI's field list. `to_dict()` is the easiest to forget — it's what
    GET /api/config returns, so a key missing there is invisible in the UI
    and unreadable by any client, while everything else still works.

    These compare the section objects' own attributes against the dict, so
    they fail for *any* future key rather than needing a line per key.
    """

    # Sections whose attributes are all non-secret and safe to expose.
    PLAIN_SECTIONS = (
        "server",
        "search",
        "download",
        "recs",
        "sync",
        "logging",
        "beets",
        "musicbrainz",
    )

    def _loaded_config(self, tmpdir):
        config_path = Path(tmpdir) / "config.toml"
        config_path.write_text("[server]\nport = 8000\n")
        config = Config(config_path=str(config_path))
        config.load()
        return config

    def test_every_attribute_is_exposed_by_to_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._loaded_config(tmpdir)
            exported = config.to_dict()

            missing = {}
            for section_name in self.PLAIN_SECTIONS:
                section = getattr(config, section_name)
                attrs = {a for a in vars(section) if not a.startswith("_")}
                gap = attrs - set(exported.get(section_name, {}))
                if gap:
                    missing[section_name] = sorted(gap)

            assert not missing, (
                f"config attributes missing from to_dict() (so invisible to "
                f"GET /api/config and the config UI): {missing}"
            )

    def test_to_dict_does_not_invent_keys(self):
        """The reverse: a key in to_dict() with no backing attribute is a
        typo that silently reports a stale or wrong value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._loaded_config(tmpdir)
            exported = config.to_dict()

            extra = {}
            for section_name in self.PLAIN_SECTIONS:
                section = getattr(config, section_name)
                attrs = {a for a in vars(section) if not a.startswith("_")}
                gap = set(exported.get(section_name, {})) - attrs
                if gap:
                    extra[section_name] = sorted(gap)

            assert not extra, f"to_dict() exports keys with no attribute: {extra}"

    def test_p65_keys_are_exposed(self):
        """Explicit coverage for the keys added by the P6.5 review, since
        these are the ones the live harness reads back over the API."""
        with tempfile.TemporaryDirectory() as tmpdir:
            exported = self._loaded_config(tmpdir).to_dict()

            assert exported["search"]["pass_ratio_threshold"] == 0.75
            assert exported["download"]["pending_timeout_minutes"] == 5
            assert exported["download"]["orphan_grace_polls"] == 2
            assert exported["download"]["manual_gate_minutes"] == 10
            # P6.7-7: rotation threshold — the completeness tests above catch
            # any future key automatically; this pins the default.
            assert exported["recs"]["rotation_trash_rating"] == 1


class TestRotationTrashRating:
    """P6.7-7: recs.rotation_trash_rating wiring + validation."""

    def _load(self, tmpdir, value=None):
        from pathlib import Path

        config_path = Path(tmpdir) / "config.toml"
        section = (
            f"[recs]\nrotation_trash_rating = {value}\n"
            if value is not None
            else "[recs]\n"
        )
        config_path.write_text(section)
        config = Config(config_path=str(config_path))
        config.load()
        return config

    def test_defaults_to_one(self, tmpdir):
        assert self._load(str(tmpdir)).recs.rotation_trash_rating == 1

    def test_reads_configured_value(self, tmpdir):
        assert self._load(str(tmpdir), 3).recs.rotation_trash_rating == 3

    def test_rejects_above_five(self, tmpdir):
        with pytest.raises(ConfigValidationError, match="rotation_trash_rating"):
            self._load(str(tmpdir), 6)

    def test_rejects_negative(self, tmpdir):
        with pytest.raises(ConfigValidationError, match="rotation_trash_rating"):
            self._load(str(tmpdir), -1)


class TestHistoryClearInterval:
    """download.history_clear_interval_minutes wiring (P6.9-7)."""

    def _load(self, tmpdir, value=None):
        from pathlib import Path

        config_path = Path(tmpdir) / "config.toml"
        section = (
            f"[download]\nhistory_clear_interval_minutes = {value}\n"
            if value is not None
            else "[download]\n"
        )
        config_path.write_text(section)
        config = Config(config_path=str(config_path))
        config.load()
        return config

    def test_defaults_to_fifteen(self, tmpdir):
        assert self._load(str(tmpdir)).download.history_clear_interval_minutes == 15

    def test_reads_configured_value(self, tmpdir):
        assert self._load(str(tmpdir), 5).download.history_clear_interval_minutes == 5

    def test_zero_disables(self, tmpdir):
        assert self._load(str(tmpdir), 0).download.history_clear_interval_minutes == 0

    def test_rejects_negative(self, tmpdir):
        with pytest.raises(ConfigValidationError, match="history_clear_interval_minutes"):
            self._load(str(tmpdir), -1)

    def test_exposed_via_to_dict(self, tmpdir):
        exported = self._load(str(tmpdir)).to_dict()
        assert exported["download"]["history_clear_interval_minutes"] == 15


class TestMusicBrainzSearchOfficialOnly:
    """musicbrainz.search_official_only wiring — default on, overridable."""

    def _load(self, tmpdir, value=None):
        from pathlib import Path

        config_path = Path(tmpdir) / "config.toml"
        section = (
            f"[musicbrainz]\nsearch_official_only = {value}\n"
            if value is not None
            else "[musicbrainz]\n"
        )
        config_path.write_text(section)
        config = Config(config_path=str(config_path))
        config.load()
        return config

    def test_defaults_to_true(self, tmpdir):
        assert self._load(str(tmpdir)).musicbrainz.search_official_only is True

    def test_reads_configured_false(self, tmpdir):
        assert (
            self._load(str(tmpdir), "false").musicbrainz.search_official_only is False
        )

    def test_reads_configured_true(self, tmpdir):
        assert self._load(str(tmpdir), "true").musicbrainz.search_official_only is True

    def test_exposed_via_to_dict(self, tmpdir):
        exported = self._load(str(tmpdir)).to_dict()
        assert exported["musicbrainz"]["search_official_only"] is True
