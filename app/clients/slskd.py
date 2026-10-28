"""Thin client for slskd's HTTP API (#78).

Raw calls only: no readiness decisions, rate cap, throttle detection or
matching. Those belong to the slskd download backend that sits on top of
this client (architecture.md, "Swappable sources").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import requests

# slskd's own default (100) silently truncates a peer's file list; keep
# everything a peer sends.
_FILE_LIMIT = 100_000


class SlskdError(Exception):
    """An slskd call failed: connection error, timeout or unexpected status."""


class SlskdBusy(SlskdError):
    """slskd refused a call because it's busy or rate-limiting (409/429)."""


@dataclass(frozen=True)
class AppState:
    logged_in: bool
    # distributed-network parent; without one searches reach almost nobody
    has_parent: bool


@dataclass(frozen=True)
class SearchState:
    id: str
    query: str
    state: str  # raw slskd state, e.g. "InProgress", "Completed, TimedOut"
    is_complete: bool
    response_count: int
    file_count: int


@dataclass(frozen=True)
class PeerFile:
    filename: str  # full peer path, backslashes as sent
    size: int
    extension: str
    length: int | None  # seconds; None when the peer didn't report it
    bitrate: int | None
    bit_depth: int | None
    sample_rate: int | None
    locked: bool


@dataclass(frozen=True)
class PeerResponse:
    username: str
    has_free_slot: bool
    upload_speed: int  # bytes/s as reported by the peer
    queue_length: int
    files: tuple[PeerFile, ...]  # every file, locked ones included


class SlskdClientBase(ABC):
    @abstractmethod
    def app_state(self) -> AppState: ...

    @abstractmethod
    def start_search(self, query: str, response_limit: int, timeout_s: int) -> str:
        """Start a search and return its id."""

    @abstractmethod
    def get_search(self, search_id: str) -> SearchState: ...

    @abstractmethod
    def get_responses(self, search_id: str) -> list[PeerResponse]:
        """Every response slskd holds for the search; re-readable at any time."""

    @abstractmethod
    def cancel_search(self, search_id: str) -> None:
        """Stop a running search; slskd keeps it and its responses."""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def parse_file(raw: dict[str, Any]) -> PeerFile:
    return PeerFile(
        filename=raw["filename"],
        size=raw.get("size", 0),
        extension=raw.get("extension") or "",
        length=_int(raw.get("length")),
        bitrate=_int(raw.get("bitRate")),
        bit_depth=_int(raw.get("bitDepth")),
        sample_rate=_int(raw.get("sampleRate")),
        locked=bool(raw.get("isLocked")),
    )


def parse_response(raw: dict[str, Any]) -> PeerResponse:
    files = (raw.get("files") or []) + (raw.get("lockedFiles") or [])
    return PeerResponse(
        username=raw["username"],
        has_free_slot=bool(raw.get("hasFreeUploadSlot")),
        upload_speed=raw.get("uploadSpeed") or 0,
        queue_length=raw.get("queueLength") or 0,
        files=tuple(parse_file(f) for f in files),
    )


def parse_search(raw: dict[str, Any]) -> SearchState:
    return SearchState(
        id=raw["id"],
        query=raw.get("searchText", ""),
        state=raw.get("state", ""),
        is_complete=bool(raw.get("isComplete")),
        response_count=raw.get("responseCount", 0),
        file_count=raw.get("fileCount", 0),
    )


class SlskdClient(SlskdClientBase):
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 15) -> None:
        self._base = base_url.rstrip("/") + "/api/v0"
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = api_key

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._session.request(
                method, self._base + path, timeout=self._timeout, **kwargs
            )
        except requests.RequestException as e:
            raise SlskdError(f"{method} {path}: {e}") from e
        if resp.status_code in (409, 429):
            raise SlskdBusy(f"{method} {path}: HTTP {resp.status_code}")
        if not resp.ok:
            raise SlskdError(
                f"{method} {path}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json() if resp.content else None

    def app_state(self) -> AppState:
        data = self._call("GET", "/application")
        return AppState(
            logged_in=bool((data.get("server") or {}).get("isLoggedIn")),
            has_parent=bool((data.get("distributedNetwork") or {}).get("hasParent")),
        )

    def start_search(self, query: str, response_limit: int, timeout_s: int) -> str:
        body = {
            "searchText": query,
            "responseLimit": response_limit,
            "searchTimeout": timeout_s * 1000,
            "fileLimit": _FILE_LIMIT,
        }
        return str(self._call("POST", "/searches", json=body)["id"])

    def get_search(self, search_id: str) -> SearchState:
        return parse_search(self._call("GET", f"/searches/{search_id}"))

    def get_responses(self, search_id: str) -> list[PeerResponse]:
        data = self._call("GET", f"/searches/{search_id}/responses")
        return [parse_response(r) for r in data or []]

    def cancel_search(self, search_id: str) -> None:
        self._call("PUT", f"/searches/{search_id}")
