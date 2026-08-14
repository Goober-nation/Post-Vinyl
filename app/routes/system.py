"""
System API routes.

Endpoints:
    GET /api/system/ping                 -> Liveness only, no dependencies (200)
    GET /api/system/status               -> Service health, version, uptime (200)
    POST /api/system/slskd/reconnect     -> Trigger slskd reconnect to Soulseek (200)
    POST /api/system/listenbrainz/check  -> On-demand ListenBrainz connectivity check (200)
    POST /api/system/sync                 -> Run both sync workers immediately (200)
    GET /api/logs                        -> Recent log lines (200, text/plain)

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

import os
import signal
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app import APP_VERSION
from app.config import Config
from app.db.database import Database
from app.db.download_store import DownloadStore
from app.dependencies import (
    get_config,
    get_db_or_none,
    get_download_service,
    get_search_service,
)
from app.exceptions import ServiceConnectionError
from app.logging_config import get_logger, get_recent_logs
from app.services import health
from app.services.beets import BeetsService
from app.services.interfaces.download import DownloadService
from app.services.interfaces.search import SearchService

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["system"])

ConfigDep = Annotated[Config, Depends(get_config)]
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
DownloadServiceDep = Annotated[DownloadService, Depends(get_download_service)]

ACTIVE_TRANSFER_STATES = {"queued", "downloading"}


# ============================================================================
# Response models
# ============================================================================


class ServiceHealthResponse(BaseModel):
    """Health status for a single service (API response shape)."""

    status: str
    latency_ms: int | None
    error: str | None


class SystemStatusResponse(BaseModel):
    """Response shape for GET /api/system/status."""

    services: dict[str, ServiceHealthResponse]
    version: str
    uptime_seconds: int
    restart_available: bool


class StopActivityResponse(BaseModel):
    """Response shape for POST /api/system/stop-slskd-activity."""

    cancelled_searches: int
    cancelled_transfers: int
    failed_transfers: int


class SyncNowResponse(BaseModel):
    """Response shape for POST /api/system/sync."""

    love_sync: dict
    trash_purge: dict


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/system/ping", include_in_schema=False)
async def system_ping() -> dict:
    """Liveness. Answers from the event loop and touches nothing else.

    `/api/system/status` is not a liveness probe and must not be used as
    one. It makes live HTTP calls to slskd and Navidrome, so its worst case
    is ~15s (slskd GET 5s + the reconnect PUT it fires when slskd reports
    itself disconnected 5s + Navidrome 5s) — all of it time musica spends
    perfectly alive, waiting on somebody else. Anything with a shorter
    timeout than that reads third-party latency as "musica is down":

      - the Dockerfile HEALTHCHECK, at a 4s timeout, would flip the
        container to unhealthy because slskd was slow;
      - the live suite's session fixture, at 5s, aborts a whole scenario
        group with "musica is not answering".

    `async def` on purpose: a sync handler is dispatched to the anyio
    worker threadpool, so it would queue behind whatever sync endpoints are
    already blocked there and stop reporting liveness at exactly the moment
    liveness became interesting.

    No dependencies either — `ConfigDep` alone would drag config loading
    into the probe. If this returns 200, the process is up and its event
    loop is turning, which is the only claim it makes.
    """
    return {"ok": True}


@router.get("/system/status", response_model=SystemStatusResponse)
def system_status(request: Request, config: ConfigDep) -> dict:
    """Return service health, application version, and uptime.

    slskd/Navidrome are checked live; ListenBrainz is reported from the last
    on-demand check (POST /api/system/listenbrainz/check) — see
    health.check_all()'s docstring for why it isn't live-checked here too.
    """
    logger.debug("GET /api/system/status")

    results = health.check_all(
        config, listenbrainz_cached=request.app.state.listenbrainz_last_check
    )

    services: dict[str, dict] = {}
    for name, h in results.items():
        services[name] = {
            "status": h.status,
            "latency_ms": h.latency_ms,
            "error": h.error,
        }

    uptime_seconds = int(time.monotonic() - request.app.state.started_at)

    return {
        "services": services,
        "version": APP_VERSION,
        "uptime_seconds": uptime_seconds,
        "restart_available": _running_in_docker(),
    }


def _running_in_docker() -> bool:
    """
    True when a restart request will actually bring the app back up.
    Only meaningful under Compose's `restart: unless-stopped` policy
    (see docker-compose.yml) — outside Docker, exiting just kills the
    process with nothing to relaunch it.
    """
    return Path("/.dockerenv").exists()


@router.post("/system/restart")
def restart_app() -> dict:
    """
    Exit the process so Docker Compose's `restart: unless-stopped` policy
    relaunches it with freshly-loaded config/.env. Response is sent before
    the process actually exits (small delay on a background thread) so the
    client sees a clean 200 instead of a dropped connection.

    SIGTERM is tried first for a clean uvicorn/worker shutdown, but uvicorn's
    graceful shutdown waits indefinitely for any still-open connection — a
    live SSE stream (GET /api/events) on a client tab left open would hang
    the restart forever. If the process hasn't exited a few seconds after
    SIGTERM, force-kill it so the restart always completes.
    """
    logger.warning("POST /api/system/restart — requesting process restart")
    pid = os.getpid()

    def _delayed_exit() -> None:
        time.sleep(0.5)
        os.kill(pid, signal.SIGTERM)
        time.sleep(3.0)
        logger.warning("restart: graceful shutdown didn't finish in time, forcing exit")
        os._exit(1)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"restarting": True}


@router.post("/system/sync", response_model=SyncNowResponse)
def sync_now(request: Request) -> dict:
    """Run the love and trash synchronization workers immediately."""
    love_sync = getattr(request.app.state, "love_sync", None)
    trash_purge = getattr(request.app.state, "trash_purge", None)
    if love_sync is None or trash_purge is None:
        raise ServiceConnectionError("Sync workers are not available")

    logger.info("POST /api/system/sync")
    return {
        "love_sync": love_sync.run_once(),
        "trash_purge": trash_purge.run_once(),
    }


@router.post("/system/slskd/reconnect")
def slskd_reconnect(config: ConfigDep) -> dict:
    """Trigger a (re)connect to the Soulseek network via slskd."""
    logger.info("POST /api/system/slskd/reconnect")
    success = health.reconnect_slskd(config)
    return {"success": success}


@router.post("/system/peers/unblock")
def unblock_all_peers(db: Annotated[Database | None, Depends(get_db_or_none)]) -> dict:
    """Clear every blocked-peer entry and reset failure counts.

    Manual override for the peer-ban list — useful after a burst of
    connectivity-caused failures (see DownloadMonitor's
    _CONNECTIVITY_FAIL_REASONS) banned peers that weren't actually at fault,
    or just to start clean. Does not affect in-flight transfers.
    """
    logger.info("POST /api/system/peers/unblock")
    if db is None:
        return {"unblocked": 0}
    return {"unblocked": DownloadStore(db).unblock_all_peers()}


@router.post("/system/consolidate")
def consolidate_albums(config: ConfigDep) -> dict:
    """Run the beets album-consolidation sweep (P6.9): regroup every live
    item by album identity and unify each group — tracks of one album that
    fragmented across trees, differently-spelled album directories, or
    duplicate copies are brought to a single canonical home, retagged to
    the canonical spelling, and renumbered from the MusicBrainz tracklist
    when the release is known. Moves/deletes files inside the music tree.
    """
    logger.info("POST /api/system/consolidate")
    result = BeetsService(config).consolidate_all()
    logger.info(
        "consolidation sweep: %s",
        {k: v for k, v in result.items() if k != "skipped"},
    )
    return result


@router.post("/system/listenbrainz/check", response_model=ServiceHealthResponse)
def listenbrainz_check(request: Request, config: ConfigDep) -> dict:
    """On-demand ListenBrainz connectivity check.

    Runs the live check and caches the result on app.state so the next
    GET /api/system/status reports it without re-hitting ListenBrainz
    (which often routes through musica-proxy's SOCKS5 upstream and can be
    slow) — see health.check_all()'s docstring.
    """
    logger.info("POST /api/system/listenbrainz/check")
    result = health.check_listenbrainz(config)
    request.app.state.listenbrainz_last_check = result
    return {
        "status": result.status,
        "latency_ms": result.latency_ms,
        "error": result.error,
    }


@router.post("/system/stop-slskd-activity", response_model=StopActivityResponse)
def stop_slskd_activity(
    search_service: SearchServiceDep,
    download_service: DownloadServiceDep,
) -> dict:
    """
    Stop everything currently talking to slskd, regardless of origin:
    cancels every in-progress search and every queued/downloading
    transfer (manual or rec-originated — recs-specific state, including
    the LB-calling pull loop itself, is handled separately by
    POST /api/recs/abort).
    """
    logger.info("POST /api/system/stop-slskd-activity")

    cancelled_searches = 0
    for job in search_service.list_searches():
        if job.status != "searching":
            continue
        try:
            if search_service.cancel(job.search_id):
                cancelled_searches += 1
        except Exception as e:  # noqa: BLE001 — cancel() impls can raise various errors
            logger.warning(
                "stop-slskd-activity: failed to cancel search %s: %s", job.search_id, e
            )

    cancelled_transfers = 0
    failed_transfers = 0
    for t in download_service.get_status():
        if t.state not in ACTIVE_TRANSFER_STATES:
            continue
        try:
            if download_service.cancel(t.transfer_id):
                cancelled_transfers += 1
            else:
                failed_transfers += 1
        except Exception as e:  # noqa: BLE001 — cancel() impls can raise various errors
            logger.warning(
                "stop-slskd-activity: failed to cancel transfer %s: %s",
                t.transfer_id,
                e,
            )
            failed_transfers += 1

    logger.info(
        "stop-slskd-activity: %d searches cancelled, %d transfers cancelled, %d transfer cancels failed",
        cancelled_searches,
        cancelled_transfers,
        failed_transfers,
    )

    return {
        "cancelled_searches": cancelled_searches,
        "cancelled_transfers": cancelled_transfers,
        "failed_transfers": failed_transfers,
    }


@router.get("/logs", response_class=PlainTextResponse)
def get_logs(limit: int = Query(100, ge=1, le=1000)) -> str:
    """Return recent application log lines as plain text."""
    logger.info("GET /api/logs")

    lines = get_recent_logs(limit)
    return "\n".join(lines) + "\n"
