"""Thin client for slskd's HTTP API (#78).

Raw calls only: no readiness decisions, rate cap, throttle detection or
matching. Those belong to the slskd download backend that sits on top of
this client (architecture.md, "Swappable sources").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

# slskd answers a queue call only after negotiating with the peer, which can
# take 20 s+ through an indirect connection; a shorter timeout reports a false
# failure (legacy finding).
_QUEUE_TIMEOUT_S = 45

# slskd stops a search at 10,000 files by default (SearchRequest, 0.26.1);
# keep everything peers send.
_FILE_LIMIT = 100_000


class SlskdError(Exception):
    """An slskd call failed: connection error, timeout or unexpected status."""


class SlskdBusy(SlskdError):
    """slskd can't take the call now; retry later (409/429). 429: another
    search start or queue call is still running (slskd allows one at a time).
    409 on start_search: slskd isn't connected/logged in to Soulseek."""


class SlskdNotFound(SlskdError):
    """HTTP 404: unknown search or transfer id, or (queue_download) the peer
    is offline."""


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


@dataclass(frozen=True)
class Transfer:
    id: str
    username: str
    filename: str  # full peer path, as queued
    state: str  # raw slskd state, e.g. "Queued, Remotely", "Completed, Succeeded"
    size: int
    bytes_transferred: int
    average_speed: int  # bytes/s
    exception: str | None  # slskd's reason for a failed or rejected transfer


@dataclass(frozen=True)
class QueueResult:
    transfers: list[Transfer]  # queued files, with their transfer ids
    failures: dict[str, str]  # filename -> slskd's message; not queued


class SlskdClientBase(ABC):
    @abstractmethod
    def app_state(self) -> AppState: ...

    @abstractmethod
    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        """Start a search and return its id. slskd ends it after
        ``idle_timeout_s`` with no new response (not a total limit)."""

    @abstractmethod
    def get_search(self, search_id: str) -> SearchState: ...

    @abstractmethod
    def get_responses(self, search_id: str) -> list[PeerResponse]:
        """Every response of the search. slskd stores them only when the search
        ends (on its own or via cancel_search), so this is ``[]`` while it runs;
        after that it can be re-read at any time."""

    @abstractmethod
    def cancel_search(self, search_id: str) -> None:
        """Stop a running search; slskd keeps it and its responses."""

    @abstractmethod
    def queue_download(
        self,
        username: str,
        files: list[tuple[str, int]],
        search_id: str | None = None,
    ) -> QueueResult:
        """Queue (filename, size) files from one peer. A file the peer doesn't
        have is still accepted here and fails later in the transfer state."""

    @abstractmethod
    def list_downloads(self) -> list[Transfer]: ...

    @abstractmethod
    def get_download(self, username: str, transfer_id: str) -> Transfer: ...

    @abstractmethod
    def cancel_download(
        self, username: str, transfer_id: str, forget: bool = False
    ) -> None:
        """Cancel a transfer; with ``forget`` slskd also drops its record.
        The downloaded file, if any, stays on disk."""


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


def parse_transfer(raw: dict[str, Any]) -> Transfer:
    return Transfer(
        id=raw["id"],
        username=raw.get("username", ""),
        filename=raw.get("filename", ""),
        state=raw.get("state", ""),
        size=raw.get("size", 0),
        bytes_transferred=raw.get("bytesTransferred", 0),
        average_speed=int(raw.get("averageSpeed") or 0),
        exception=raw.get("exception"),
    )


class SlskdClient(SlskdClientBase):
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 15) -> None:
        self._base = base_url.rstrip("/") + "/api/v0"
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers["X-API-Key"] = api_key

    def _call(
        self, method: str, path: str, timeout_s: float | None = None, **kwargs: Any
    ) -> Any:
        try:
            resp = self._session.request(
                method, self._base + path, timeout=timeout_s or self._timeout, **kwargs
            )
        except requests.RequestException as e:
            raise SlskdError(f"{method} {path}: {e}") from e
        if resp.status_code == 404:
            raise SlskdNotFound(f"{method} {path}: HTTP 404 {resp.text[:200]}")
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

    def start_search(self, query: str, response_limit: int, idle_timeout_s: int) -> str:
        body = {
            "searchText": query,
            "responseLimit": response_limit,
            # milliseconds, despite slskd's doc comment saying seconds: it's
            # passed straight to Soulseek.NET (checked live 2026-10-28)
            "searchTimeout": idle_timeout_s * 1000,
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

    def queue_download(
        self,
        username: str,
        files: list[tuple[str, int]],
        search_id: str | None = None,
    ) -> QueueResult:
        body = {
            "username": username,
            "searchId": search_id,
            "files": [{"filename": name, "size": size} for name, size in files],
        }
        # 201 all queued, 207 some failed, 200 all failed; the body lists both
        data = self._call(
            "POST", "/transfers/downloads/batches", _QUEUE_TIMEOUT_S, json=body
        )
        return QueueResult(
            transfers=[
                parse_transfer({"username": username, **t})
                for t in (data.get("batch") or {}).get("transfers") or []
            ],
            failures={f["filename"]: f.get("message", "") for f in data["failures"]},
        )

    def list_downloads(self) -> list[Transfer]:
        groups = self._call("GET", "/transfers/downloads") or []
        return [
            parse_transfer({"username": g.get("username", ""), **f})
            for g in groups
            for d in g.get("directories") or []
            for f in d.get("files") or []
        ]

    def get_download(self, username: str, transfer_id: str) -> Transfer:
        path = f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}"
        return parse_transfer({"username": username, **self._call("GET", path)})

    def cancel_download(
        self, username: str, transfer_id: str, forget: bool = False
    ) -> None:
        path = f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}"
        self._call("DELETE", path, params={"remove": "true"} if forget else None)
