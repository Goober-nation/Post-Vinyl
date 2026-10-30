"""Settings (#109, user 2026-10-30): each user's own, and the install's.

The schema says per field how to draw it (``config.SchemaField``). A user's
settings apply at once: their source order and off switches (stored by
``sources.py``), how many results a catalog search shows, and the downloads
tab's default filter and page size. The install's settings are the YAML file:
an admin edit rewrites only the changed lines (comments stay), keeps a backup
and applies at the next restart; the folders are shown, never edited.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import (
    ConfigError,
    SchemaField,
    Settings,
    admin_schema,
    check_value,
    file_values,
    running_values,
    write_values,
)
from app.services.downloads import STATES
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.users import UsersStore

SOURCE_KINDS = ("catalog", "download")  # what a user can reorder and switch off


def user_schema(prefs: SourcePreferences) -> list[SchemaField]:
    return [
        *(
            SchemaField(
                f"sources.{kind}",
                "sources",
                "sources",
                {"order": list(prefs.configured(kind)), "off": []},
                f"Your {kind} sources, in the order they're tried; switch any off.",
                choices=prefs.configured(kind),
                applies="now",
            )
            for kind in SOURCE_KINDS
        ),
        SchemaField(
            "catalog.results_per_search",
            "catalog",
            "int",
            25,
            "How many results a catalog search shows.",
            5,
            100,
            5,
            applies="now",
        ),
        SchemaField(
            "downloads.page_size",
            "downloads",
            "int",
            20,
            "Downloads shown per page.",
            10,
            100,
            10,
            applies="now",
        ),
        SchemaField(
            "downloads.state",
            "downloads",
            "choice",
            "",
            "Which downloads the tab shows first (empty: all).",
            choices=("", *sorted(STATES)),
            applies="now",
        ),
    ]


class UserSettingsBase(ABC):
    @abstractmethod
    def schema(self) -> list[SchemaField]: ...

    @abstractmethod
    def get(self, user_id: int) -> dict[str, Any]: ...

    @abstractmethod
    def set(self, user_id: int, changes: dict[str, Any]) -> dict[str, Any]: ...


class UserSettings(UserSettingsBase):
    def __init__(
        self,
        db: Database,
        prefs: SourcePreferences,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._prefs = prefs
        self._clock = clock
        self._users = UsersStore()

    def schema(self) -> list[SchemaField]:
        return user_schema(self._prefs)

    def get(self, user_id: int) -> dict[str, Any]:
        with self._db.transaction() as conn:
            stored = self._users.get_settings(conn, user_id)
        out: dict[str, Any] = {}
        for f in self.schema():
            if f.type == "sources":
                kind = f.key.split(".", 1)[1]
                on = self._prefs.for_user(user_id, kind)
                off = [s for s in self._prefs.configured(kind) if s not in on]
                out[f.key] = {"order": on + off, "off": off}
            else:
                out[f.key] = stored.get(f.key, f.default)
        return out

    def set(self, user_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        schema = {f.key: f for f in self.schema()}
        unknown = sorted(set(changes) - set(schema))
        if unknown:
            raise ConfigError(f"unknown settings {unknown}")
        checked = {
            k: self._check(schema[k], v) for k, v in changes.items()
        }  # all first
        for key, value in checked.items():
            if schema[key].type == "sources":
                self._prefs.set_for_user(user_id, key.split(".", 1)[1], value["order"],
                                         value["off"])  # fmt: skip
            else:
                with self._db.transaction(write=True) as conn:
                    self._users.set_setting(
                        conn, user_id, key, value, int(self._clock())
                    )
        return self.get(user_id)

    @staticmethod
    def _check(f: SchemaField, value: Any) -> Any:
        if f.type == "sources":
            if not isinstance(value, dict) or set(value) != {"order", "off"}:
                raise ConfigError(f"{f.key} must be {{order: [...], off: [...]}}")
            for part in ("order", "off"):
                check_value(SchemaField(f.key, f.section, "ordered_list", [], "",
                                        choices=f.choices), value[part])  # fmt: skip
            return value
        if f.type == "choice":
            if value not in f.choices:
                raise ConfigError(f"{f.key} must be one of {list(f.choices)}")
            return value
        return check_value(f, value)


@dataclass(frozen=True)
class InstallView:
    values: dict[str, Any]  # what the config file says
    restart_pending: list[str]  # keys whose file value isn't what the app runs with


class InstallSettingsBase(ABC):
    @abstractmethod
    def schema(self) -> list[SchemaField]: ...

    @abstractmethod
    def get(self) -> InstallView: ...

    @abstractmethod
    def set(self, changes: dict[str, Any]) -> InstallView: ...


class InstallSettings(InstallSettingsBase):
    def __init__(
        self, running: Settings, clock: Callable[[], float] = time.time
    ) -> None:
        self._running = running_values(running)
        self._path = Path(running.config_path)
        self._clock = clock

    def schema(self) -> list[SchemaField]:
        return admin_schema()

    def _view(self, values: dict[str, Any]) -> InstallView:
        pending = sorted(k for k, v in values.items() if self._running.get(k) != v)
        return InstallView(values, pending)

    def get(self) -> InstallView:
        return self._view(file_values(self._path))

    def set(self, changes: dict[str, Any]) -> InstallView:
        try:
            values = write_values(self._path, changes, int(self._clock()))
        except OSError as e:  # e.g. the config folder is mounted read-only
            # strerror only: the OSError text names our own folder (live #113)
            raise ConfigError(f"can't write {self._path.name}: {e.strerror}") from e
        return self._view(values)
