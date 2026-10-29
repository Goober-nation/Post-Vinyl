"""Download monitor worker (#102): calls the downloader's ``poll`` every few
seconds, so running attempts move on (queue wait, stall rule, completion)."""

from __future__ import annotations

from collections.abc import Callable

from app.workers.periodic import Periodic

POLL_EVERY_S = 2.0


class DownloadMonitor(Periodic):
    def __init__(self, poll: Callable[[], None], every_s: float = POLL_EVERY_S) -> None:
        super().__init__("download-monitor", poll, every_s)
