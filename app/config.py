"""Startup values from the environment (#88), until the config redesign.

Read once in ``main.create_app_from_env``; a missing required value refuses
to start rather than running half-configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """A required environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    db_path: str
    navidrome_url: str
    public_origin: str  # e.g. https://music.example.com; the CSRF check compares to it
    dev: bool = False  # serves /docs

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
        )
