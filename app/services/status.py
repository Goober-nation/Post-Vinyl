"""Service status for the header: ping each service the app talks to.

A source that's off for the user isn't called, like everywhere else.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from app.clients.catalog import CatalogError
from app.clients.listenbrainz import ListenBrainzError
from app.clients.navidrome import NavidromeError
from app.clients.slskd import SlskdError
from app.services.sources import SourcePreferences

log = logging.getLogger(__name__)


class UnknownService(Exception):
    """No service by that name."""


@dataclass(frozen=True)
class Check:
    name: str
    label: str
    ping: Callable[[], object]  # raises when the service can't be reached
    source: tuple[str, str] | None = None  # (kind, name) that must be on


@dataclass(frozen=True)
class ServiceInfo:
    name: str
    label: str
    main: bool  # shown as a light in the header


@dataclass(frozen=True)
class ServicePing:
    name: str
    state: str  # ok | down | off
    ms: int | None


MAIN = ("navidrome", "slskd")


class StatusServiceBase(ABC):
    @abstractmethod
    def services(self) -> list[ServiceInfo]: ...

    @abstractmethod
    def ping(self, user_id: int, name: str) -> ServicePing: ...


class StatusService(StatusServiceBase):
    def __init__(self, prefs: SourcePreferences, checks: list[Check]) -> None:
        self._prefs = prefs
        self._checks = {c.name: c for c in checks}

    def services(self) -> list[ServiceInfo]:
        return [
            ServiceInfo(c.name, c.label, c.name in MAIN) for c in self._checks.values()
        ]

    def ping(self, user_id: int, name: str) -> ServicePing:
        check = self._checks.get(name)
        if check is None:
            raise UnknownService(name)
        if check.source and not self._prefs.is_on(user_id, *check.source):
            return ServicePing(name, "off", None)
        started = time.monotonic()
        try:
            check.ping()
        except (NavidromeError, SlskdError, CatalogError, ListenBrainzError) as e:
            log.info("%s isn't reachable: %s", name, e)
            return ServicePing(name, "down", None)
        return ServicePing(name, "ok", int((time.monotonic() - started) * 1000))
