"""The core loop's background workers, started together (#105).

``start()`` first resumes what the last run left ``searching`` and starts
requests still waiting, then runs every worker on its own thread. Wiring it
into the app is milestone 7.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.config import WorkersSettings
from app.services.dispatch import DownloadDispatchBase
from app.services.pipeline import FilePipeline
from app.workers.download_monitor import DownloadMonitor
from app.workers.periodic import Periodic

log = logging.getLogger(__name__)


class CoreLoopWorkers:
    def __init__(
        self,
        settings: WorkersSettings,
        dispatch: DownloadDispatchBase,
        pipeline: FilePipeline,
        downloads_poll: Callable[[], None],
        cleanup_tick: Callable[[], object],
    ) -> None:
        self._dispatch = dispatch
        s = settings
        self.workers = [
            DownloadMonitor(downloads_poll),
            Periodic("file-pipeline", pipeline.tick, s.pipeline_every_seconds),
            Periodic("cleanup", cleanup_tick, s.cleanup_every_seconds),
            # its own worker: slskd being down never holds up the disk (live #115)
            Periodic(
                "unverifiable", pipeline.discard_unverifiable, s.cleanup_every_seconds
            ),
            Periodic(
                "missing-check", pipeline.check_missing, s.missing_check_minutes * 60
            ),
            Periodic(
                "redispatch", dispatch.redispatch_waiting, s.redispatch_every_seconds
            ),
        ]

    def start(self) -> None:
        resumed = self._dispatch.resume()
        started = self._dispatch.redispatch_waiting()
        log.info("core loop: resumed %d, started %d waiting requests", resumed, started)
        for w in self.workers:
            w.start()

    def stop(self) -> None:
        for w in self.workers:
            w.stop()
