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
class Settings:
    db_path: str
    navidrome_url: str
    public_origin: str  # e.g. https://music.example.com; the CSRF check compares to it
    dev: bool = False  # serves /docs
    # enabled sources per kind, in priority order (user, 2026-10-29)
    sources: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(KNOWN_SOURCES)
    )

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
        )


def load_sources(path: Path) -> dict[str, tuple[str, ...]]:
    """The ``sources`` lists from the YAML config, checked against KNOWN_SOURCES."""
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"can't read config {path}: {e}") from e
    raw = (data or {}).get("sources") if isinstance(data, dict) else None
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
