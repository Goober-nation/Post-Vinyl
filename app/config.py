"""Startup values (#88, #95), until the config redesign.

Secrets and deployment values come from the environment; settings come from
the YAML file at ``PV_CONFIG_PATH`` (every config file is YAML, user
2026-10-29; ``config/post-vinyl.example.yaml`` documents each value). Read
once in ``main.create_app_from_env``; a missing or invalid value refuses to
start rather than running half-configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Every source name each kind accepts; a new source adds its name here and to
# the comment in config/post-vinyl.example.yaml in the same change.
KNOWN_SOURCES: dict[str, tuple[str, ...]] = {
    "catalog": ("musicbrainz", "deezer", "listenbrainz"),
    "download": ("slskd",),
    "request": ("postradio", "yandex"),
}


class ConfigError(Exception):
    """A required environment variable is missing or invalid."""


@dataclass(frozen=True)
class SlskdSettings:
    """Advanced slskd values from the YAML ``slskd`` section (#100, user 2026-10-29)."""

    searches_per_minute: int = 4  # live 2026-10-27: 4/min held, ~5-6/min got throttled
    throttle_check_query: str = "2020"  # healthy ≈ 600 responses (research run)
    throttle_check_min_responses: int = 300
    throttle_pause_minutes: int = 10
    # downloads (#102, user 2026-10-29)
    queue_wait_seconds: int = 15  # a candidate that keeps us queued longer is skipped
    candidates_per_download: int = 5
    peer_ban_failures: int = 3
    peer_ban_days: int = 7


@dataclass(frozen=True)
class FilesSettings:
    """Folders from the YAML ``files`` section (#103)."""

    music_dir: str = "/music"  # the music folder as Post-Vinyl sees it
    # inside music_dir: where slskd saves downloads, and Navidrome's library
    slskd_downloads_dir: str = "downloads/complete/soulseek"
    library_dir: str = "library"


def load_files(path: Path) -> FilesSettings:
    raw = _read(path).get("files") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'files' must be a section")
    defaults = FilesSettings()
    unknown = set(raw) - set(vars(defaults))
    if unknown:
        raise ConfigError(f"{path}: unknown files settings {sorted(unknown)}")
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{path}: files.{name} must be a folder path")
    return FilesSettings(**raw)


@dataclass(frozen=True)
class Settings:
    db_path: str
    navidrome_url: str
    public_origin: str  # e.g. https://music.example.com; the CSRF check compares to it
    dev: bool = False  # serves /docs
    # enabled sources per kind, in priority order (user, 2026-10-29)
    sources: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(KNOWN_SOURCES)
    )
    slskd: SlskdSettings = field(default_factory=SlskdSettings)
    files: FilesSettings = field(default_factory=FilesSettings)

    @classmethod
    def from_env(cls) -> Settings:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        origin = required("PV_PUBLIC_ORIGIN").rstrip("/")
        if not origin.startswith(("https://", "http://")) or origin.count("/") != 2:
            raise ConfigError(
                f"PV_PUBLIC_ORIGIN must be scheme://host[:port], got {origin!r}"
            )
        return cls(
            db_path=required("PV_DB_PATH"),
            navidrome_url=required("PV_NAVIDROME_URL"),
            public_origin=origin,
            dev=os.environ.get("PV_DEV", "").strip().lower() in ("1", "true", "yes"),
            sources=load_sources(Path(required("PV_CONFIG_PATH"))),
            slskd=load_slskd(Path(required("PV_CONFIG_PATH"))),
            files=load_files(Path(required("PV_CONFIG_PATH"))),
        )


def _read(path: Path) -> dict[str, Any]:
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"can't read config {path}: {e}") from e
    return data if isinstance(data, dict) else {}


def load_slskd(path: Path) -> SlskdSettings:
    """The optional ``slskd`` section; a value left out keeps its default."""
    raw = _read(path).get("slskd") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'slskd' must be a section")
    defaults = SlskdSettings()
    unknown = set(raw) - set(vars(defaults))
    if unknown:
        raise ConfigError(f"{path}: unknown slskd settings {sorted(unknown)}")
    for name, value in raw.items():
        want = type(getattr(defaults, name))
        if type(value) is not want or (want is int and value < 1) or value == "":
            raise ConfigError(
                f"{path}: slskd.{name} must be a positive {want.__name__}"
            )
    return SlskdSettings(**raw)


def load_sources(path: Path) -> dict[str, tuple[str, ...]]:
    """The ``sources`` lists from the YAML config, checked against KNOWN_SOURCES."""
    raw = _read(path).get("sources")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: missing 'sources' section")
    unknown_kinds = set(raw) - set(KNOWN_SOURCES)
    if unknown_kinds:
        raise ConfigError(f"{path}: unknown source kinds {sorted(unknown_kinds)}")
    out: dict[str, tuple[str, ...]] = {}
    for kind, allowed in KNOWN_SOURCES.items():
        names = raw.get(kind) or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ConfigError(f"{path}: sources.{kind} must be a list of names")
        bad = [n for n in names if n not in allowed]
        if bad:
            raise ConfigError(
                f"{path}: sources.{kind} has unknown {bad}; possible: {list(allowed)}"
            )
        if len(set(names)) != len(names):
            raise ConfigError(f"{path}: sources.{kind} lists a source twice")
        out[kind] = tuple(names)
    return out
