"""In-memory DownloadDispatch that behaves like the real one (#99).

Fill ``requests`` (request id → user id, track_state, has a song) and set
``enabled`` to the user's download sources in order. Same rules as
``services/dispatch.py``: only the owner can act, only ``requested`` /
``reRequested`` requests with a song start, no enabled source → None and
nothing changes, the first enabled source takes it (and claims it as
``searching``, like a real source), cancel reaches every source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.dispatch import (
    STARTABLE,
    DownloadDispatchBase,
    DownloadStatus,
    NotStartable,
)
from app.services.requests import RequestNotFound


@dataclass
class FakeRequest:
    user_id: int
    track_state: str = "requested"
    has_song: bool = True


@dataclass
class FakeDownloadDispatch(DownloadDispatchBase):
    requests: dict[int, FakeRequest] = field(default_factory=dict)
    enabled: list[str] = field(default_factory=list)
    started: list[tuple[str, int]] = field(default_factory=list)  # (source, request)
    cancelled: list[int] = field(default_factory=list)

    def _get(self, user_id: int, request_id: int) -> FakeRequest:
        req = self.requests.get(request_id)
        if req is None or req.user_id != user_id:
            raise RequestNotFound(f"request {request_id}")
        return req

    def dispatch(self, user_id: int, request_id: int) -> str | None:
        req = self._get(user_id, request_id)
        if req.track_state not in STARTABLE:
            raise NotStartable(f"request {request_id} is {req.track_state}")
        if not req.has_song:
            raise NotStartable(f"request {request_id} has no song")
        if not self.enabled:
            return None
        req.track_state = "searching"
        self.started.append((self.enabled[0], request_id))
        return self.enabled[0]

    def resume(self) -> int:
        stuck = [r for r in self.requests.values() if r.track_state == "searching"]
        for req in stuck:
            req.track_state = "requested"
            if self.enabled and req.has_song:
                req.track_state = "searching"
                self.started.append((self.enabled[0], next(
                    k for k, v in self.requests.items() if v is req)))  # fmt: skip
        return len(stuck)

    def cancel(self, user_id: int, request_id: int) -> None:
        self._get(user_id, request_id)
        self.cancelled.append(request_id)

    def status(self, user_id: int, request_id: int) -> DownloadStatus:
        req = self._get(user_id, request_id)
        return DownloadStatus(req.track_state, None, None, None)
