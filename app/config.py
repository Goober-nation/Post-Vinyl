"""
Musica Configuration System

Loads configuration from TOML file with hot-reload support.
Secrets are loaded from .env file.
"""

import os
import re
from pathlib import Path

import toml

from app.exceptions import ConfigError, ConfigNotFoundError, ConfigValidationError


class Config:
    """
    Configuration manager with hot-reload support.

    Usage:
        config = Config()
        config.load()  # Load from config.toml and .env

        # Access values
        port = config.server.port
        navidrome_url = config.navidrome.url

        # Hot-reload non-secret settings
        config.reload()
    """

    def __init__(self, config_path: str | None = None, env_path: str | None = None):
        """
        Initialize Config.

        Args:
            config_path: Path to config.toml (default: ./config.toml)
            env_path: Path to .env file (default: ./.env)
        """
        self.config_path = Path(config_path) if config_path else Path("config.toml")
        self.env_path = Path(env_path) if env_path else Path(".env")

        # Sections
        self.server = ServerConfig()
        self.paths = PathsConfig()
        self.navidrome = NavidromeConfig()
        self.slskd = SlskdConfig()
        self.listenbrainz = ListenBrainzConfig()
        self.search = SearchConfig()
        self.download = DownloadConfig()
        self.recs = RecsConfig()
        self.fresh_picks = FreshPicksConfig()
        self.sync = SyncConfig()
        self.logging = LoggingConfig()
        self.auth = AuthConfig()
        self.beets = BeetsConfig()
        self.musicbrainz = MusicBrainzConfig()

        self._loaded = False
        self._config_mtime_ns: int | None = None

    def load(self):
        """
        Load configuration from TOML file and .env.

        Raises:
            ConfigNotFoundError: If config.toml not found
            ConfigValidationError: If config validation fails
        """
        if not self.config_path.exists():
            raise ConfigNotFoundError(str(self.config_path))

        # Load TOML
        try:
            with open(self.config_path, "r") as f:
                data = toml.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"Failed to parse config.toml: {e}")

        # Load .env for secrets
        self._load_env()

        # Populate sections
        self._populate_server(data.get("server", {}))
        self._populate_paths(data.get("paths", {}))
        self._populate_navidrome(data.get("navidrome", {}))
        self._populate_slskd(data.get("slskd", {}))
        self._populate_listenbrainz(data.get("listenbrainz", {}))
        self._populate_search(data.get("search", {}))
        self._populate_download(data.get("download", {}))
        self._populate_recs(data.get("recs", {}))
        self._populate_fresh_picks(data.get("fresh_picks", {}))
        self._populate_sync(data.get("sync", {}))
        self._populate_logging(data.get("logging", {}))
        self._populate_auth()
        self._populate_beets(data.get("beets", {}))
        self._populate_musicbrainz(data.get("musicbrainz", {}))

        # Validate
        self._validate()

        self._loaded = True
        self._remember_config_mtime()

    def reload(self):
        """
        Reload non-secret configuration (hot-reload).
        Secrets remain unchanged (require restart).
        """
        if not self._loaded:
            raise ConfigError("Config not loaded yet. Call load() first.")

        # Reload TOML only (not .env)
        try:
            with open(self.config_path, "r") as f:
                data = toml.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"Failed to parse config.toml: {e}")

        # Reload non-secret sections
        self._populate_search(data.get("search", {}))
        self._populate_download(data.get("download", {}))
        self._populate_recs(data.get("recs", {}))
        self._populate_fresh_picks(data.get("fresh_picks", {}))
        self._populate_sync(data.get("sync", {}))
        self._populate_logging(data.get("logging", {}))
        self._populate_beets(data.get("beets", {}))
        self._populate_musicbrainz(data.get("musicbrainz", {}))

        # Validate
        self._validate()
        self._remember_config_mtime()

    def reload_if_changed(self) -> bool:
        """Reload hot-reloadable settings when config.toml changed externally."""
        if not self._loaded:
            return False

        try:
            mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError as e:
            raise ConfigError(f"Failed to stat config.toml: {e}")

        if mtime_ns == self._config_mtime_ns:
            return False

        self.reload()
        return True

    def write_env_values(self, values: dict[str, str]) -> None:
        """Write raw KEY=value pairs into .env, preserving unrelated lines.

        Shared by /api/config/secrets and /api/setup/* — both write .env
        entries by key, replacing an existing line for that key or appending
        a new one. Does not reload Config; secrets require a restart.
        """
        existing_lines: list[str] = []
        if self.env_path.exists():
            existing_lines = self.env_path.read_text().splitlines()

        processed_keys: set[str] = set()
        new_lines: list[str] = []
        for line in existing_lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else stripped
            if key and key in values:
                new_lines.append(f"{key}={values[key]}")
                processed_keys.add(key)
            else:
                new_lines.append(line)

        for key, value in values.items():
            if key not in processed_keys:
                new_lines.append(f"{key}={value}")

        self.env_path.write_text("\n".join(new_lines) + "\n")

    def _remember_config_mtime(self) -> None:
        """Record the current config file version after a successful load."""
        try:
            self._config_mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError as e:
            raise ConfigError(f"Failed to stat config.toml: {e}")

    def _load_env(self):
        """Load secrets from .env file."""
        if not self.env_path.exists():
            return  # .env is optional

        with open(self.env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                os.environ[key] = value

    def _get_env(self, key: str, default: str = "") -> str:
        """Get environment variable."""
        return os.environ.get(key, default)

    def _populate_server(self, data: dict):
        """Populate server config section."""
        self.server.port = self._get_int(data, "port", 8000)
        self.server.host = self._get_str(data, "host", "0.0.0.0")

    def _populate_paths(self, data: dict):
        """Populate paths config section.

        music_dir is the Docker mount root — set only via config.toml/env,
        never through the config API (not in PathsSettings in
        app/routes/config.py). The directory settings are relative suffixes
        under it, user-editable; the resolved absolute paths are
        PathsConfig.*_path properties.
        """
        self.paths.data_dir = Path(self._get_str(data, "data_dir", "/app/data"))
        self.paths.music_dir = Path(self._get_str(data, "music_dir", "/music"))
        self.paths.download_dir = self._get_str(data, "download_dir", "downloads")
        self.paths.searches_dir = self._get_str(data, "searches_dir", "Searches")
        self.paths.library_dir = self._get_str(data, "library_dir", "library")
        self.paths.discovery_familiar_dir = self._get_str(
            data, "discovery_familiar_dir", "Discovery/Comfort_Zone"
        )
        self.paths.discovery_new_releases_dir = self._get_str(
            data, "discovery_new_releases_dir", "Discovery/Fresh_Picks"
        )
        self.paths.discovery_exploration_dir = self._get_str(
            data, "discovery_exploration_dir", "Discovery/Deep_Cuts"
        )

    def _populate_navidrome(self, data: dict):
        """Populate navidrome config section."""
        self.navidrome.url = self._get_str(data, "url", "http://navidrome-server:4533")
        # Secrets from .env
        self.navidrome.username = self._get_env("NAVIDROME_USERNAME", "")
        self.navidrome.password = self._get_env("NAVIDROME_PASSWORD", "")

    def _populate_slskd(self, data: dict):
        """Populate slskd config section."""
        self.slskd.url = self._get_str(data, "url", "http://slskd:5030")
        # Secrets from .env
        self.slskd.api_key = self._get_env("SLSKD_API_KEY", "")
        self.slskd.network_username = self._get_env("SLSKD_NETWORK_USERNAME", "")
        self.slskd.network_password = self._get_env("SLSKD_NETWORK_PASSWORD", "")

    def _populate_listenbrainz(self, data: dict):
        """Populate listenbrainz config section.

        No `enabled` flag — enabled is derived from username/token being
        present (see ListenBrainzConfig.enabled), which only happens once
        both are set via the secrets panel and the app is restarted.
        """
        self.listenbrainz.url = self._get_str(
            data, "url", "https://api.listenbrainz.org"
        )
        # Secrets from .env
        self.listenbrainz.token = self._get_env("LISTENBRAINZ_TOKEN", "")
        self.listenbrainz.username = self._get_env("LISTENBRAINZ_USERNAME", "")

    def _populate_auth(self):
        """Populate auth config section (secrets only, from .env)."""
        self.auth.username = self._get_env("MUSICA_AUTH_USERNAME", "")
        self.auth.password = self._get_env("MUSICA_AUTH_PASSWORD", "")

    def _populate_search(self, data: dict):
        """Populate search config section."""
        self.search.wait_seconds = self._get_int(data, "wait_seconds", 10)
        self.search.poll_interval = self._get_int(data, "poll_interval", 1)
        self.search.response_threshold = self._get_int(data, "response_threshold", 10)
        self.search.response_cap = self._get_int(data, "response_cap", 250)
        self.search.min_wait_seconds = self._get_int(data, "min_wait_seconds", 3)
        self.search.pass_ratio_threshold = self._get_float(
            data, "pass_ratio_threshold", 0.75
        )
        self.search.artist_match_min_words = self._get_int(
            data, "artist_match_min_words", 1
        )
        # Diagnosed 2026-08-13: one Soulseek search fans out to thousands of
        # peer connections; response_limit caps what slskd *records* (it does
        # not reduce the connection count — measured directly, see
        # live-artifacts/hang-probe-1), while the rate limiter and query
        # cache below bound how often a new search happens at all, which is
        # the thing that actually mattered.
        self.search.response_limit = self._get_int(data, "response_limit", 60)
        self.search.rate_limit_max_searches = self._get_int(
            data, "rate_limit_max_searches", 4
        )
        self.search.rate_limit_window_seconds = self._get_int(
            data, "rate_limit_window_seconds", 60
        )
        self.search.rate_limit_wait_timeout_seconds = self._get_int(
            data, "rate_limit_wait_timeout_seconds", 45
        )
        self.search.query_cache_ttl_seconds = self._get_int(
            data, "query_cache_ttl_seconds", 600
        )

    def _populate_download(self, data: dict):
        """Populate download config section."""
        self.download.check_interval = self._get_int(data, "check_interval", 15)
        self.download.max_retries_per_track = self._get_int(
            data, "max_retries_per_track", 3
        )
        self.download.bad_peer_threshold = self._get_int(data, "bad_peer_threshold", 1)
        self.download.upload_limit_mb = self._get_int(data, "upload_limit_mb", 50)
        self.download.pending_timeout_minutes = self._get_int(
            data, "pending_timeout_minutes", 5
        )
        self.download.orphan_grace_polls = self._get_int(data, "orphan_grace_polls", 2)
        self.download.manual_gate_minutes = self._get_int(
            data, "manual_gate_minutes", 10
        )
        self.download.peer_ban_days = self._get_int(data, "peer_ban_days", 2)
        self.download.missing_source_timeout_minutes = self._get_int(
            data, "missing_source_timeout_minutes", 5
        )
        self.download.history_clear_interval_minutes = self._get_int(
            data, "history_clear_interval_minutes", 15
        )

    def _populate_recs(self, data: dict):
        """Populate recs config section."""
        if "interval_hours" in data:
            raise ConfigValidationError(
                "recs.interval_hours",
                data["interval_hours"],
                "no longer supported (P6.5-2) — replace with recs.comfort_zone_interval_days "
                "and recs.deep_cuts_interval_days; Fresh Picks uses its nightly cadence",
            )
        if "enabled" in data:
            raise ConfigValidationError(
                "recs.enabled",
                data["enabled"],
                "no longer supported (P6.5-3b) — the single master switch was replaced by "
                "per-category recs.comfort_zone_enabled / recs.fresh_picks_enabled / "
                "recs.deep_cuts_enabled",
            )
        self.recs.comfort_zone_enabled = self._get_bool(
            data, "comfort_zone_enabled", False
        )
        self.recs.fresh_picks_enabled = self._get_bool(
            data, "fresh_picks_enabled", False
        )
        self.recs.deep_cuts_enabled = self._get_bool(data, "deep_cuts_enabled", False)
        self.recs.comfort_zone_interval_days = self._get_int(
            data, "comfort_zone_interval_days", 1
        )
        self.recs.deep_cuts_interval_days = self._get_int(
            data, "deep_cuts_interval_days", 7
        )
        # P6.7-1: three fully independent playlist names (decision over a
        # shared base name + fixed suffixes).
        self.recs.comfort_zone_playlist_name = self._get_str(
            data, "comfort_zone_playlist_name", "Comfort Zone"
        )
        self.recs.fresh_picks_playlist_name = self._get_str(
            data, "fresh_picks_playlist_name", "Fresh Picks"
        )
        self.recs.deep_cuts_playlist_name = self._get_str(
            data, "deep_cuts_playlist_name", "Deep Cuts"
        )
        self.recs.comfort_zone_count = self._get_int(data, "comfort_zone_count", 5)
        self.recs.deep_cuts_count = self._get_int(data, "deep_cuts_count", 5)
        # P6.7-7: rotation threshold. A rec-sourced track rated at or below
        # this (and any unrated track) is moved to the Trash playlist when
        # its category rotates; tracks rated above it are kept in the
        # library and only removed from the playlist. Default 1 = the
        # "rated <2★ or unrated → Trash" rule from the backlog.
        self.recs.rotation_trash_rating = self._get_int(
            data, "rotation_trash_rating", 1
        )

    def _populate_fresh_picks(self, data: dict):
        """Populate Fresh Picks' rolling-window settings.

        ``[fresh_picks].count`` is the single source of truth for the
        nightly target size (2026-08-13: the old ``recs.fresh_picks_count``
        alias was removed — the Recs tab edits this section directly).
        """
        self.fresh_picks.pull_window = self._get_str(data, "pull_window", "30d")
        self.fresh_picks.offset = self._get_int(data, "offset", 50)
        self.fresh_picks.count = self._get_int(data, "count", 5)
        self.fresh_picks.search_buffer = self._get_int(data, "search_buffer", 25)

    def _populate_sync(self, data: dict):
        """Populate sync config section."""
        self.sync.interval_hours = self._get_int(data, "interval_hours", 12)

    def _populate_logging(self, data: dict):
        """Populate logging config section."""
        self.logging.level = self._get_str(data, "level", "INFO")
        self.logging.format = self._get_str(
            data, "format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    def _populate_beets(self, data: dict):
        """Populate beets config section (P6.6-1)."""
        self.beets.enabled = self._get_bool(data, "enabled", False)
        self.beets.binary = self._get_str(data, "binary", "beet")
        self.beets.timeout_seconds = self._get_int(data, "timeout_seconds", 120)

    def _populate_musicbrainz(self, data: dict):
        """Populate musicbrainz config section (P-MB-1).

        `min_request_interval` defaults to MusicBrainz's published 1 req/sec
        limit for anonymous clients. Lowering it risks a block on the
        project's whole IP, so it is exposed for raising, not lowering.
        """
        self.musicbrainz.enabled = self._get_bool(data, "enabled", True)
        self.musicbrainz.url = self._get_str(data, "url", "https://musicbrainz.org")
        self.musicbrainz.timeout_seconds = self._get_int(data, "timeout_seconds", 15)
        self.musicbrainz.min_request_interval = self._get_float(
            data, "min_request_interval", 1.0
        )
        self.musicbrainz.cache_ttl_seconds = self._get_int(
            data, "cache_ttl_seconds", 3600
        )
        self.musicbrainz.min_score = self._get_int(data, "min_score", 90)
        self.musicbrainz.search_official_only = self._get_bool(
            data, "search_official_only", True
        )

    def _get_str(self, data: dict, key: str, default: str) -> str:
        """Get string value from dict."""
        return str(data.get(key, default))

    def _get_int(self, data: dict, key: str, default: int) -> int:
        """Get integer value from dict."""
        value = data.get(key, default)
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _get_float(self, data: dict, key: str, default: float) -> float:
        """Get float value from dict."""
        value = data.get(key, default)
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _get_bool(self, data: dict, key: str, default: bool) -> bool:
        """Get boolean value from dict."""
        value = data.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "yes", "1")
        return default

    def _validate(self):
        """Validate configuration values."""
        # Server
        if self.server.port < 1 or self.server.port > 65535:
            raise ConfigValidationError(
                "server.port", self.server.port, "Must be 1-65535"
            )

        # Search
        if self.search.wait_seconds < 1:
            raise ConfigValidationError(
                "search.wait_seconds", self.search.wait_seconds, "Must be >= 1"
            )
        if self.search.poll_interval < 1:
            raise ConfigValidationError(
                "search.poll_interval", self.search.poll_interval, "Must be >= 1"
            )
        if self.search.response_threshold < 1:
            raise ConfigValidationError(
                "search.response_threshold",
                self.search.response_threshold,
                "Must be >= 1",
            )
        if self.search.response_cap < 1:
            raise ConfigValidationError(
                "search.response_cap", self.search.response_cap, "Must be >= 1"
            )
        if self.search.min_wait_seconds < 0:
            raise ConfigValidationError(
                "search.min_wait_seconds", self.search.min_wait_seconds, "Must be >= 0"
            )
        if not 0 < self.search.pass_ratio_threshold <= 1:
            raise ConfigValidationError(
                "search.pass_ratio_threshold",
                self.search.pass_ratio_threshold,
                "Must be in (0, 1]",
            )
        if self.search.artist_match_min_words < 1:
            raise ConfigValidationError(
                "search.artist_match_min_words",
                self.search.artist_match_min_words,
                "Must be >= 1",
            )
        if self.search.response_limit < 1:
            raise ConfigValidationError(
                "search.response_limit", self.search.response_limit, "Must be >= 1"
            )
        if self.search.rate_limit_max_searches < 1:
            raise ConfigValidationError(
                "search.rate_limit_max_searches",
                self.search.rate_limit_max_searches,
                "Must be >= 1",
            )
        if self.search.rate_limit_window_seconds < 1:
            raise ConfigValidationError(
                "search.rate_limit_window_seconds",
                self.search.rate_limit_window_seconds,
                "Must be >= 1",
            )
        if self.search.rate_limit_wait_timeout_seconds < 0:
            raise ConfigValidationError(
                "search.rate_limit_wait_timeout_seconds",
                self.search.rate_limit_wait_timeout_seconds,
                "Must be >= 0",
            )
        if self.search.query_cache_ttl_seconds < 0:
            raise ConfigValidationError(
                "search.query_cache_ttl_seconds",
                self.search.query_cache_ttl_seconds,
                "Must be >= 0",
            )

        # Download
        if self.download.check_interval < 1:
            raise ConfigValidationError(
                "download.check_interval", self.download.check_interval, "Must be >= 1"
            )
        if self.download.max_retries_per_track < 0:
            raise ConfigValidationError(
                "download.max_retries_per_track",
                self.download.max_retries_per_track,
                "Must be >= 0",
            )
        if self.download.bad_peer_threshold < 1:
            raise ConfigValidationError(
                "download.bad_peer_threshold",
                self.download.bad_peer_threshold,
                "Must be >= 1",
            )
        if self.download.upload_limit_mb < 1:
            raise ConfigValidationError(
                "download.upload_limit_mb",
                self.download.upload_limit_mb,
                "Must be >= 1",
            )
        if self.download.pending_timeout_minutes < 1:
            raise ConfigValidationError(
                "download.pending_timeout_minutes",
                self.download.pending_timeout_minutes,
                "Must be >= 1",
            )
        if self.download.orphan_grace_polls < 1:
            raise ConfigValidationError(
                "download.orphan_grace_polls",
                self.download.orphan_grace_polls,
                "Must be >= 1",
            )
        if self.download.manual_gate_minutes < 1:
            raise ConfigValidationError(
                "download.manual_gate_minutes",
                self.download.manual_gate_minutes,
                "Must be >= 1",
            )
        if self.download.peer_ban_days < 1:
            raise ConfigValidationError(
                "download.peer_ban_days",
                self.download.peer_ban_days,
                "Must be >= 1",
            )
        if self.download.missing_source_timeout_minutes < 1:
            raise ConfigValidationError(
                "download.missing_source_timeout_minutes",
                self.download.missing_source_timeout_minutes,
                "Must be >= 1",
            )
        if self.download.history_clear_interval_minutes < 0:
            raise ConfigValidationError(
                "download.history_clear_interval_minutes",
                self.download.history_clear_interval_minutes,
                "Must be >= 0 (0 disables automatic history cleanup)",
            )

        # Recs
        if self.recs.comfort_zone_interval_days < 1:
            raise ConfigValidationError(
                "recs.comfort_zone_interval_days",
                self.recs.comfort_zone_interval_days,
                "Must be >= 1",
            )
        if self.recs.deep_cuts_interval_days < 1:
            raise ConfigValidationError(
                "recs.deep_cuts_interval_days",
                self.recs.deep_cuts_interval_days,
                "Must be >= 1",
            )
        if self.recs.comfort_zone_count < 0:
            raise ConfigValidationError(
                "recs.comfort_zone_count", self.recs.comfort_zone_count, "Must be >= 0"
            )
        if self.recs.deep_cuts_count < 0:
            raise ConfigValidationError(
                "recs.deep_cuts_count", self.recs.deep_cuts_count, "Must be >= 0"
            )
        if not 0 <= self.recs.rotation_trash_rating <= 5:
            raise ConfigValidationError(
                "recs.rotation_trash_rating",
                self.recs.rotation_trash_rating,
                "Must be 0-5",
            )
        for key in (
            "comfort_zone_playlist_name",
            "fresh_picks_playlist_name",
            "deep_cuts_playlist_name",
        ):
            value = getattr(self.recs, key)
            if not value or not value.strip():
                raise ConfigValidationError(f"recs.{key}", value, "Must not be empty")

        if not _DURATION_RE.fullmatch(self.fresh_picks.pull_window.strip()):
            raise ConfigValidationError(
                "fresh_picks.pull_window",
                self.fresh_picks.pull_window,
                "Must be a positive duration such as '1d' or '24h'",
            )
        if self.fresh_picks.window_seconds <= 0:
            raise ConfigValidationError(
                "fresh_picks.pull_window",
                self.fresh_picks.pull_window,
                "Must be greater than zero",
            )
        if self.fresh_picks.window_days > 90:
            raise ConfigValidationError(
                "fresh_picks.pull_window",
                self.fresh_picks.pull_window,
                "ListenBrainz supports a maximum 90-day window",
            )
        if self.fresh_picks.offset < 0:
            raise ConfigValidationError(
                "fresh_picks.offset", self.fresh_picks.offset, "Must be >= 0"
            )
        if self.fresh_picks.count < 0:
            raise ConfigValidationError(
                "fresh_picks.count", self.fresh_picks.count, "Must be >= 0"
            )
        if self.fresh_picks.search_buffer < 0:
            raise ConfigValidationError(
                "fresh_picks.search_buffer",
                self.fresh_picks.search_buffer,
                "Must be >= 0",
            )

        # Sync
        if self.sync.interval_hours < 1:
            raise ConfigValidationError(
                "sync.interval_hours", self.sync.interval_hours, "Must be >= 1"
            )

        # Logging
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.logging.level.upper() not in valid_levels:
            raise ConfigValidationError(
                "logging.level", self.logging.level, f"Must be one of {valid_levels}"
            )

        # Beets
        if self.beets.timeout_seconds < 1:
            raise ConfigValidationError(
                "beets.timeout_seconds", self.beets.timeout_seconds, "Must be >= 1"
            )
        if not self.beets.binary.strip():
            raise ConfigValidationError(
                "beets.binary", self.beets.binary, "Must not be empty"
            )

        # MusicBrainz
        if not 0 <= self.musicbrainz.min_score <= 100:
            raise ConfigValidationError(
                "musicbrainz.min_score",
                self.musicbrainz.min_score,
                "Must be 0-100",
            )
        if self.musicbrainz.min_request_interval <= 0:
            raise ConfigValidationError(
                "musicbrainz.min_request_interval",
                self.musicbrainz.min_request_interval,
                "Must be > 0",
            )
        if self.musicbrainz.timeout_seconds < 1:
            raise ConfigValidationError(
                "musicbrainz.timeout_seconds",
                self.musicbrainz.timeout_seconds,
                "Must be >= 1",
            )
        if self.musicbrainz.cache_ttl_seconds < 0:
            raise ConfigValidationError(
                "musicbrainz.cache_ttl_seconds",
                self.musicbrainz.cache_ttl_seconds,
                "Must be >= 0",
            )

    def to_dict(self) -> dict:
        """Convert config to dict (for debugging/API responses)."""
        return {
            "server": {"port": self.server.port, "host": self.server.host},
            "paths": {
                "data_dir": str(self.paths.data_dir),
                "music_dir": str(self.paths.music_dir),
                "download_dir": self.paths.download_dir,
                "searches_dir": self.paths.searches_dir,
                "library_dir": self.paths.library_dir,
                "discovery_familiar_dir": self.paths.discovery_familiar_dir,
                "discovery_new_releases_dir": self.paths.discovery_new_releases_dir,
                "discovery_exploration_dir": self.paths.discovery_exploration_dir,
                "discovery_familiar_path": str(self.paths.discovery_familiar_path),
                "discovery_new_releases_path": str(
                    self.paths.discovery_new_releases_path
                ),
                "discovery_exploration_path": str(
                    self.paths.discovery_exploration_path
                ),
            },
            "navidrome": {
                "url": self.navidrome.url,
                "username": "***" if self.navidrome.username else "",
                "password": "***" if self.navidrome.password else "",
            },
            "slskd": {
                "url": self.slskd.url,
                "api_key": "***" if self.slskd.api_key else "",
            },
            "listenbrainz": {
                "enabled": self.listenbrainz.enabled,
                "url": self.listenbrainz.url,
                "token": "***" if self.listenbrainz.token else "",
                "username": self.listenbrainz.username,
            },
            "search": {
                "wait_seconds": self.search.wait_seconds,
                "poll_interval": self.search.poll_interval,
                "response_threshold": self.search.response_threshold,
                "response_cap": self.search.response_cap,
                "min_wait_seconds": self.search.min_wait_seconds,
                "pass_ratio_threshold": self.search.pass_ratio_threshold,
                "artist_match_min_words": self.search.artist_match_min_words,
                "response_limit": self.search.response_limit,
                "rate_limit_max_searches": self.search.rate_limit_max_searches,
                "rate_limit_window_seconds": self.search.rate_limit_window_seconds,
                "rate_limit_wait_timeout_seconds": self.search.rate_limit_wait_timeout_seconds,
                "query_cache_ttl_seconds": self.search.query_cache_ttl_seconds,
            },
            "download": {
                "check_interval": self.download.check_interval,
                "max_retries_per_track": self.download.max_retries_per_track,
                "bad_peer_threshold": self.download.bad_peer_threshold,
                "upload_limit_mb": self.download.upload_limit_mb,
                "pending_timeout_minutes": self.download.pending_timeout_minutes,
                "orphan_grace_polls": self.download.orphan_grace_polls,
                "manual_gate_minutes": self.download.manual_gate_minutes,
                "peer_ban_days": self.download.peer_ban_days,
                "missing_source_timeout_minutes": self.download.missing_source_timeout_minutes,
                "history_clear_interval_minutes": self.download.history_clear_interval_minutes,
            },
            "recs": {
                "comfort_zone_enabled": self.recs.comfort_zone_enabled,
                "fresh_picks_enabled": self.recs.fresh_picks_enabled,
                "deep_cuts_enabled": self.recs.deep_cuts_enabled,
                "comfort_zone_interval_days": self.recs.comfort_zone_interval_days,
                "deep_cuts_interval_days": self.recs.deep_cuts_interval_days,
                "comfort_zone_playlist_name": self.recs.comfort_zone_playlist_name,
                "fresh_picks_playlist_name": self.recs.fresh_picks_playlist_name,
                "deep_cuts_playlist_name": self.recs.deep_cuts_playlist_name,
                "comfort_zone_count": self.recs.comfort_zone_count,
                "deep_cuts_count": self.recs.deep_cuts_count,
                "rotation_trash_rating": self.recs.rotation_trash_rating,
            },
            "fresh_picks": {
                "pull_window": self.fresh_picks.pull_window,
                "offset": self.fresh_picks.offset,
                "count": self.fresh_picks.count,
                "search_buffer": self.fresh_picks.search_buffer,
            },
            "sync": {"interval_hours": self.sync.interval_hours},
            "logging": {"level": self.logging.level, "format": self.logging.format},
            "beets": {
                "enabled": self.beets.enabled,
                "binary": self.beets.binary,
                "timeout_seconds": self.beets.timeout_seconds,
            },
            "musicbrainz": {
                "enabled": self.musicbrainz.enabled,
                "url": self.musicbrainz.url,
                "timeout_seconds": self.musicbrainz.timeout_seconds,
                "min_request_interval": self.musicbrainz.min_request_interval,
                "cache_ttl_seconds": self.musicbrainz.cache_ttl_seconds,
                "min_score": self.musicbrainz.min_score,
                "version": self.musicbrainz.version,
                "search_official_only": self.musicbrainz.search_official_only,
            },
            "auth": {
                "enabled": self.auth.enabled,
                "username": "***" if self.auth.username else "",
                "password": "***" if self.auth.password else "",
            },
        }


# ============================================================================
# Config Section Classes
# ============================================================================


class ServerConfig:
    """Server configuration."""

    def __init__(self):
        self.port: int = 8000
        self.host: str = "0.0.0.0"


class PathsConfig:
    """Paths configuration.

    music_dir is the fixed Docker mount root (not user-editable via the
    API). The directory settings are relative suffixes under it — use the
    *_path properties for the resolved absolute paths. The per-category
    discovery dirs are nested under the Discovery tree (P6.7-0b); their
    parent is derived from those category paths rather than configured
    separately.
    """

    def __init__(self):
        self.data_dir: Path = Path("/app/data")
        self.music_dir: Path = Path("/music")
        self.download_dir: str = "downloads"
        self.searches_dir: str = "Searches"
        self.library_dir: str = "library"
        self.discovery_familiar_dir: str = "Discovery/Comfort_Zone"
        self.discovery_new_releases_dir: str = "Discovery/Fresh_Picks"
        self.discovery_exploration_dir: str = "Discovery/Deep_Cuts"

    @property
    def download_path(self) -> Path:
        return self.music_dir / self.download_dir

    @property
    def slskd_downloads_path(self) -> Path:
        """Where slskd must write completed downloads for DownloadMonitor to
        find them (see app/workers/download_monitor.py's
        _resolve_source_path()). Single source of truth for both the
        bootstrap startup check and the SLSKD_DOWNLOADS_DIR value written to
        .env when download_dir changes."""
        return self.download_path / "complete" / "soulseek"

    @property
    def searches_path(self) -> Path:
        return self.music_dir / self.searches_dir

    @property
    def library_path(self) -> Path:
        return self.music_dir / self.library_dir

    @property
    def discovery_familiar_path(self) -> Path:
        return self.music_dir / self.discovery_familiar_dir

    @property
    def discovery_new_releases_path(self) -> Path:
        return self.music_dir / self.discovery_new_releases_dir

    @property
    def discovery_exploration_path(self) -> Path:
        return self.music_dir / self.discovery_exploration_dir


class NavidromeConfig:
    """Navidrome configuration."""

    def __init__(self):
        self.url: str = "http://navidrome-server:4533"
        self.username: str = ""
        self.password: str = ""


class SlskdConfig:
    """slskd configuration."""

    def __init__(self):
        self.url: str = "http://slskd:5030"
        self.api_key: str = ""
        # Read-only status signal for the setup wizard — musica never uses
        # these to talk to slskd itself (slskd owns the Soulseek connection).
        self.network_username: str = ""
        self.network_password: str = ""


class ListenBrainzConfig:
    """ListenBrainz configuration."""

    def __init__(self):
        self.url: str = "https://api.listenbrainz.org"
        self.token: str = ""
        self.username: str = ""

    @property
    def enabled(self) -> bool:
        """Derived from credentials — set username/token via secrets to enable."""
        return bool(self.token and self.username)


class SearchConfig:
    """Search configuration."""

    def __init__(self):
        self.wait_seconds: int = 10
        self.poll_interval: int = 1
        self.response_threshold: int = 10
        self.response_cap: int = 250
        self.min_wait_seconds: int = 3
        # P6.5-6: pass-ratio threshold for the re-query ladder — a query
        # rung counts as a hit when this fraction of its results survive
        # the client-side filters. Raised 0.6 -> 0.75 on 2026-08-11 after
        # live measurement: real rungs scored 0.81-0.93, so at 0.6 the first
        # rung always won and the re-query ladder never engaged at all.
        self.pass_ratio_threshold: float = 0.75

        # How many of the artist's words a candidate filename must contain
        # to survive the ladder's artist-containment filter. Default 1 keeps
        # the long-standing "any one artist word" behaviour; raise it to be
        # stricter. Never rejects a candidate just for the artist having
        # fewer words than this — the effective requirement is
        # min(artist_match_min_words, len(artist_words)).
        self.artist_match_min_words: int = 1

        # Diagnosed 2026-08-13: bounds on the cost of a Soulseek search.
        # response_limit caps what slskd *records* per search (it does not
        # reduce the peer connections opened — measured, not a lever on its
        # own) but there's no reason to keep more than needed, so it's set
        # well under slskd's own 250 default. The rate limiter and query
        # cache are what actually matter: no more than
        # rate_limit_max_searches new searches may start within any
        # rate_limit_window_seconds (SlskdSearch blocks callers for up to
        # rate_limit_wait_timeout_seconds rather than failing outright), and
        # an identical query string within query_cache_ttl_seconds is served
        # from the prior search's responses instead of broadcasting again.
        self.response_limit: int = 60
        self.rate_limit_max_searches: int = 4
        self.rate_limit_window_seconds: int = 60
        self.rate_limit_wait_timeout_seconds: int = 45
        self.query_cache_ttl_seconds: int = 600


class DownloadConfig:
    """Download configuration."""

    def __init__(self):
        self.check_interval: int = 15
        self.max_retries_per_track: int = 3
        self.bad_peer_threshold: int = 1
        self.upload_limit_mb: int = 50
        # How long a queue-time pending row may sit unadopted by slskd
        # before it's marked failed. Adoption normally happens within one
        # check_interval, so this is generous; the point is that a row
        # slskd never picks up must not gate rec queueing forever.
        self.pending_timeout_minutes: int = 5
        # Consecutive polls a slskd-adopted transfer may go unreported
        # before it's treated as orphaned (failed + retried). >1 so a
        # single truncated or blipped status response can't kill healthy
        # transfers.
        self.orphan_grace_polls: int = 2
        # How long a manual download that is still merely *queued* holds the
        # rec-queue priority gate. Rows actually 'downloading' are never
        # aged out. Guards against a peer parking the transfer in its own
        # upload queue — routine on Soulseek — starving recs indefinitely.
        self.manual_gate_minutes: int = 10
        # How long a bad-peer block lasts before it's lifted automatically
        # and the peer's failure_count resets to 0 — a peer that failed
        # once months ago must not stay banned forever (2026-08-12: a long-
        # running session found the permanent ban exhausting the viable
        # peer pool over several days). Checked lazily against
        # peers.blocked_at at query time (DownloadStore.is_peer_blocked),
        # not by a periodic sweep.
        self.peer_ban_days: int = 2
        # How long DownloadMonitor keeps retrying to locate a completed
        # transfer's file on disk before giving up and marking it failed.
        # Normally this resolves within one check_interval; the timeout
        # exists for the case where it never will — e.g. a stale row
        # adopted from slskd's own transfer history after a reset wiped the
        # file trees but not slskd's memory of them, pointing at a file
        # that will never exist. Without a cutoff that row retries forever,
        # once per check_interval, logging the same warning indefinitely.
        self.missing_source_timeout_minutes: int = 5
        # How often (minutes) slskd's terminal transfer history (completed /
        # failed / cancelled) is cleared automatically (P6.9-7). 0 disables.
        # Found live 2026-08-14: slskd's accumulated download history
        # congested the stack; clearing it restored normal operation. Local
        # musica bookkeeping rows are deliberately kept — only slskd-side
        # records are removed, and failed removals are retried next cycle.
        self.history_clear_interval_minutes: int = 15


_DURATION_RE = re.compile(r"(?i)(?:[1-9][0-9]*)(?:d|h)")


class FreshPicksConfig:
    """Fresh Picks rolling-window and nightly pull settings."""

    def __init__(self):
        self.pull_window: str = "30d"
        self.offset: int = 50
        self.count: int = 5
        self.search_buffer: int = 25

    @property
    def window_seconds(self) -> int:
        """Return the configured window in seconds."""
        value = self.pull_window.strip().lower()
        if not _DURATION_RE.fullmatch(value):
            return 0
        amount = int(value[:-1])
        return amount * (86400 if value.endswith("d") else 3600)

    @property
    def window_days(self) -> int:
        """Return whole API days, rounding a partial day up."""
        seconds = self.window_seconds
        return (seconds + 86399) // 86400 if seconds else 0


class RecsConfig:
    """Recommendations configuration.

    Comfort Zone and Deep Cuts have user-controlled intervals. Fresh Picks
    runs on its own nightly cadence and keeps its fill settings in
    :class:`FreshPicksConfig`.

    P6.5-3b: the single master `enabled` switch was replaced by a per-
    category `*_enabled` flag for each of the 3 categories (there are 3
    independent playlists now, not 1). These flags control periodic pulls;
    the Recs UI's explicit manual category selection is independent.
    """

    def __init__(self):
        self.comfort_zone_enabled: bool = False
        self.fresh_picks_enabled: bool = False
        self.deep_cuts_enabled: bool = False
        self.comfort_zone_interval_days: int = 1
        self.deep_cuts_interval_days: int = 7
        self.comfort_zone_playlist_name: str = "Comfort Zone"
        self.fresh_picks_playlist_name: str = "Fresh Picks"
        self.deep_cuts_playlist_name: str = "Deep Cuts"
        self.comfort_zone_count: int = 5
        self.deep_cuts_count: int = 5
        # P6.7-7: rotation threshold (see _populate_recs for semantics).
        self.rotation_trash_rating: int = 1


class SyncConfig:
    """Sync configuration."""

    def __init__(self):
        self.interval_hours: int = 12


class LoggingConfig:
    """Logging configuration."""

    def __init__(self):
        self.level: str = "INFO"
        self.format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class AuthConfig:
    """HTTP Basic Auth configuration (single user)."""

    def __init__(self):
        self.username: str = ""
        self.password: str = ""

    @property
    def enabled(self) -> bool:
        """Derived from credentials — set both via .env to require auth."""
        return bool(self.username and self.password)


class BeetsConfig:
    """Beets configuration (P6.6-1) — subprocess-invoked import/tag/move.

    `enabled` gates whether DownloadMonitor routes completed transfers
    through beets at all; when disabled, downloads are left in place
    (untagged) rather than falling back to the retired `_move_file()`.
    """

    def __init__(self):
        self.enabled: bool = False
        self.binary: str = "beet"
        self.timeout_seconds: int = 120


class MusicBrainzConfig:
    """MusicBrainz configuration (P-MB-1) — canonical metadata lookups.

    MusicBrainz requires a descriptive User-Agent. The client identifies the
    project with its public URL; no personal contact setting is needed.

    `min_request_interval` is MusicBrainz's published anonymous rate limit
    (1 req/sec, averaged). It is exposed so a self-hosted mirror can raise
    the rate — lowering it against the public server risks having the whole
    IP blocked.

    `min_score` is the confidence floor below which a search result is
    discarded rather than applied. It is deliberately high: a weak match
    applied silently files the track under a confident-looking wrong name,
    which is worse than leaving it flagged as unmatched.
    """

    def __init__(self):
        self.enabled: bool = True
        self.url: str = "https://musicbrainz.org"
        self.timeout_seconds: int = 15
        self.min_request_interval: float = 1.0
        self.cache_ttl_seconds: int = 3600
        self.min_score: int = 90
        self.version: str = "0.1"
        # Search & discovery: restrict the MusicBrainz search tab to official
        # releases (albums/singles/EPs) by default — hides mixtapes, bootlegs,
        # live albums, compilations, DJ-mixes, demos and remixes. Flip off to
        # browse everything MusicBrainz has.
        self.search_official_only: bool = True
