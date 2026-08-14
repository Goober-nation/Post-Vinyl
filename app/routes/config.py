"""
Config API routes.

Endpoints:
    GET  /api/config          -> Full config (200), secrets masked
    POST /api/config          -> Update config sections (200)
    POST /api/config/secrets  -> Update secrets in .env (200)

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import toml
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Config
from app.dependencies import get_config, get_event_hub
from app.exceptions import ConfigValidationError
from app.logging_config import get_logger
from app.sse import EventHub

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["config"])

ConfigDep = Annotated[Config, Depends(get_config)]
EventHubDep = Annotated[EventHub, Depends(get_event_hub)]

HOT_SECTIONS = {
    "search",
    "download",
    "recs",
    "fresh_picks",
    "sync",
    "logging",
    "beets",
}

SECRET_ENV_KEYS = {
    "navidrome_username": "NAVIDROME_USERNAME",
    "navidrome_password": "NAVIDROME_PASSWORD",
    "slskd_api_key": "SLSKD_API_KEY",
    "listenbrainz_token": "LISTENBRAINZ_TOKEN",
    "listenbrainz_username": "LISTENBRAINZ_USERNAME",
}


# ============================================================================
# Request models
# ============================================================================


class PathsSettings(BaseModel):
    """Paths config section fields.

    music_dir is deliberately absent — it's the Docker mount root and
    changing it from the frontend would break the container's volumes, so
    it's config.toml/env-only. The rest are relative suffixes joined under
    music_dir server-side (see PathsConfig.*_path in app/config.py).
    """

    model_config = ConfigDict(extra="forbid")

    data_dir: str | None = None
    download_dir: str | None = None
    searches_dir: str | None = None
    library_dir: str | None = None
    discovery_familiar_dir: str | None = None
    discovery_new_releases_dir: str | None = None
    discovery_exploration_dir: str | None = None

    @field_validator(
        "download_dir",
        "searches_dir",
        "library_dir",
        "discovery_familiar_dir",
        "discovery_new_releases_dir",
        "discovery_exploration_dir",
    )
    @classmethod
    def _validate_relative_subdir(cls, value: str | None) -> str | None:
        """Reject anything that isn't a plain relative path under music_dir."""
        if value is None:
            return None
        value = value.strip().strip("/")
        if not value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("must be a relative path with no '..' components")
        return value


class NavidromeSettings(BaseModel):
    """Navidrome config section fields."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None


class SlskdSettings(BaseModel):
    """slskd config section fields."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = None


class SearchSettings(BaseModel):
    """Search config section fields."""

    model_config = ConfigDict(extra="forbid")

    wait_seconds: int | None = Field(None, ge=1)
    response_threshold: int | None = Field(None, ge=1)
    response_cap: int | None = Field(None, ge=1)
    min_wait_seconds: int | None = Field(None, ge=0)
    pass_ratio_threshold: float | None = Field(None, gt=0, le=1)
    artist_match_min_words: int | None = Field(None, ge=1)


class DownloadSettings(BaseModel):
    """Download config section fields."""

    model_config = ConfigDict(extra="forbid")

    check_interval: int | None = Field(None, ge=1)
    max_retries_per_track: int | None = Field(None, ge=0)
    bad_peer_threshold: int | None = Field(None, ge=1)
    upload_limit_mb: int | None = Field(None, ge=1)
    pending_timeout_minutes: int | None = Field(None, ge=1)
    orphan_grace_polls: int | None = Field(None, ge=1)
    manual_gate_minutes: int | None = Field(None, ge=1)
    missing_source_timeout_minutes: int | None = Field(None, ge=1)
    history_clear_interval_minutes: int | None = Field(None, ge=0)


class RecsSettings(BaseModel):
    """Recommendation config section fields."""

    model_config = ConfigDict(extra="forbid")

    comfort_zone_enabled: bool | None = None
    fresh_picks_enabled: bool | None = None
    deep_cuts_enabled: bool | None = None
    comfort_zone_interval_days: int | None = Field(None, ge=1)
    deep_cuts_interval_days: int | None = Field(None, ge=1)
    comfort_zone_playlist_name: str | None = Field(None, min_length=1)
    fresh_picks_playlist_name: str | None = Field(None, min_length=1)
    deep_cuts_playlist_name: str | None = Field(None, min_length=1)
    comfort_zone_count: int | None = Field(None, ge=0)
    deep_cuts_count: int | None = Field(None, ge=0)
    rotation_trash_rating: int | None = Field(None, ge=0, le=5)


class FreshPicksSettings(BaseModel):
    """Fresh Picks rolling-window settings."""

    model_config = ConfigDict(extra="forbid")

    pull_window: str | None = Field(None, min_length=2)
    offset: int | None = Field(None, ge=0)
    count: int | None = Field(None, ge=0)
    search_buffer: int | None = Field(None, ge=0)

    @field_validator("pull_window")
    @classmethod
    def _validate_pull_window(cls, value: str | None) -> str | None:
        """Reject durations the config loader cannot turn into API days."""
        if value is not None:
            value = value.strip().lower()
            if not re.fullmatch(r"[1-9][0-9]*(?:d|h)", value):
                raise ValueError("must be a positive duration such as '1d' or '24h'")
        return value


class SyncSettings(BaseModel):
    """Sync config section fields."""

    model_config = ConfigDict(extra="forbid")

    interval_hours: int | None = Field(None, ge=1)


class LoggingSettings(BaseModel):
    """Logging config section fields."""

    model_config = ConfigDict(extra="forbid")

    level: str | None = None
    format: str | None = None

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str | None) -> str | None:
        """Strip, uppercase, and validate logging level."""
        if value is None:
            return None
        value = value.strip().upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                "level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"
            )
        return value


class BeetsSettings(BaseModel):
    """Beets config section fields."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    binary: str | None = Field(None, min_length=1)
    timeout_seconds: int | None = Field(None, ge=1)


class MusicBrainzSettings(BaseModel):
    """MusicBrainz config section fields."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    url: str | None = None
    timeout_seconds: int | None = Field(None, ge=1)
    min_request_interval: float | None = Field(None, gt=0)
    cache_ttl_seconds: int | None = Field(None, ge=0)
    min_score: int | None = Field(None, ge=0, le=100)
    search_official_only: bool | None = None


class ConfigUpdateRequest(BaseModel):
    """Body for POST /api/config — partial section updates."""

    model_config = ConfigDict(extra="forbid")

    paths: PathsSettings | None = None
    navidrome: NavidromeSettings | None = None
    slskd: SlskdSettings | None = None
    search: SearchSettings | None = None
    download: DownloadSettings | None = None
    recs: RecsSettings | None = None
    fresh_picks: FreshPicksSettings | None = None
    sync: SyncSettings | None = None
    logging: LoggingSettings | None = None
    beets: BeetsSettings | None = None
    musicbrainz: MusicBrainzSettings | None = None


_SECRET_FIELD_NAMES = list(SECRET_ENV_KEYS)


class SecretsUpdateRequest(BaseModel):
    """Body for POST /api/config/secrets."""

    model_config = ConfigDict(extra="forbid")

    navidrome_username: str | None = None
    navidrome_password: str | None = None
    slskd_api_key: str | None = None
    listenbrainz_token: str | None = None
    listenbrainz_username: str | None = None

    @field_validator(*_SECRET_FIELD_NAMES, mode="before")
    @classmethod
    def _validate_secret_value(cls, value: str | None) -> str | None:
        """Reject empty, newlines, or double quotes in secret values."""
        if value is None:
            return None
        value = value.strip() if isinstance(value, str) else str(value)
        if not value:
            raise ValueError("value must not be empty")
        if "\n" in value or "\r" in value or '"' in value:
            raise ValueError("value must not contain newlines or double quotes")
        return value


# ============================================================================
# Response models
# ============================================================================


class ConfigResponse(BaseModel):
    """Response shape for POST /api/config."""

    config: dict
    requires_restart: list[str]


class SecretsResponse(BaseModel):
    """Response shape for POST /api/config/secrets."""

    updated: list[str]
    requires_restart: bool


# ============================================================================
# Helpers
# ============================================================================


@contextmanager
def _config_backup_guard(config: Config) -> Generator[None, None, None]:
    """Backup config.toml before writing; restore on ConfigValidationError."""
    backup = config.config_path.read_bytes()
    try:
        yield
    except ConfigValidationError:
        config.config_path.write_bytes(backup)
        config.reload()
        raise


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/config", response_model=dict)
def get_full_config(
    config: ConfigDep,
) -> dict:
    """Return full configuration with secrets masked."""
    logger.info("GET /api/config")
    return config.to_dict()


@router.post("/config", response_model=ConfigResponse)
def update_config(
    body: ConfigUpdateRequest,
    config: ConfigDep,
    event_hub: EventHubDep,
) -> dict:
    """Update configuration sections via hot-reload or restart."""
    payload = body.model_dump(exclude_none=True)

    if not payload:
        logger.info("POST /api/config: no sections provided")
        return {"config": config.to_dict(), "requires_restart": []}

    logger.info(f"POST /api/config: sections={list(payload)}")

    # Load existing TOML
    with open(config.config_path) as f:
        data = toml.load(f)

    # Merge each provided section
    for section_name in payload:
        section_data = data.setdefault(section_name, {})
        section_payload = payload[section_name]
        section_data.update(section_payload)

    # Write with rollback guard
    with _config_backup_guard(config):
        with open(config.config_path, "w") as f:
            toml.dump(data, f)
        config.reload()

    # download_dir changed -> slskd's SLSKD_DOWNLOADS_DIR must move with it,
    # or completed transfers silently vanish (see
    # app.services.bootstrap.check_slskd_download_dir). Both need a restart
    # anyway ("paths" isn't in HOT_SECTIONS), so writing this now means the
    # one restart the user already has to do picks up a consistent pair.
    if "download_dir" in payload.get("paths", {}):
        config.write_env_values(
            {"SLSKD_DOWNLOADS_DIR": str(config.paths.slskd_downloads_path)}
        )

    requires_restart = [n for n in payload if n not in HOT_SECTIONS]

    event_hub.publish(
        "system.config_reloaded",
        {"changed_keys": [f"{s}.{k}" for s, vals in payload.items() for k in vals]},
    )

    return {"config": config.to_dict(), "requires_restart": requires_restart}


@router.post("/config/secrets", response_model=SecretsResponse)
def update_secrets(
    body: SecretsUpdateRequest,
    config: ConfigDep,
) -> dict:
    """Update secrets in .env file (requires restart)."""
    provided = {
        env_key: value
        for field, env_key in SECRET_ENV_KEYS.items()
        if (value := getattr(body, field, None)) is not None
    }

    if not provided:
        logger.info("POST /api/config/secrets: no secrets provided")
        return {"updated": [], "requires_restart": True}

    logger.info(f"POST /api/config/secrets: keys={sorted(provided)}")

    config.write_env_values(provided)

    # ListenBrainz scrobbling has no env var / config.toml equivalent —
    # ND_LISTENBRAINZ_TOKEN in docker-compose.yml is not a real Navidrome
    # config key and has always been a silent no-op (confirmed live
    # 2026-08-14). The only way to enable it is Navidrome's own
    # PUT /api/listenbrainz/link, called here so saving the token actually
    # does something instead of requiring the user to separately paste it
    # into Navidrome's own Personal Settings UI. Best-effort: Navidrome
    # might not be reachable yet (e.g. mid-wizard, before its admin account
    # restart) or the admin credentials might not be saved yet — either
    # just means the user still has to do it by hand in Navidrome's UI,
    # same as before this existed.
    if "LISTENBRAINZ_TOKEN" in provided and config.navidrome.username:
        try:
            from app.services.navidrome_library import NavidromeLibrary

            linked = NavidromeLibrary(config).link_listenbrainz(
                provided["LISTENBRAINZ_TOKEN"]
            )
            if not linked:
                logger.warning(
                    "Could not auto-enable Navidrome ListenBrainz scrobbling — "
                    "enable it by hand in Navidrome's Personal Settings"
                )
        except Exception:
            logger.warning(
                "Navidrome ListenBrainz auto-link failed", exc_info=True
            )

    return {"updated": sorted(provided), "requires_restart": True}
