"""
Recommendation API routes.

Endpoints:
    GET  /api/recs/status    -> Settings + status counts + last/next pull + running (200)
    POST /api/recs/pull      -> Trigger a manual pull (202 started / 409 already running)
    POST /api/recs/settings  -> Update recs config section (200, hot-reload)
    GET  /api/recs/pending   -> List recommendation rows, optional status filter (200)

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers, except
the 409 REC_PULL_IN_PROGRESS case below which needs a semantic (non-HTTP_xxx)
error code and is returned directly via JSONResponse.
"""

from datetime import datetime, timezone
from typing import Annotated, Literal

import toml
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import Config
from app.db.database import Database
from app.db.recs_store import RecsStore
from app.dependencies import (
    get_config,
    get_db,
    get_download_service,
    get_event_hub,
    get_rec_puller,
)
from app.logging_config import get_logger
from app.routes.config import (
    FreshPicksSettings,
    RecsSettings,
    _config_backup_guard,
)
from app.services.interfaces.download import DownloadService
from app.services.recs_data import RecsDataService
from app.sse import EventHub
from app.workers.rec_puller import RecPuller

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["recs"])

ConfigDep = Annotated[Config, Depends(get_config)]
EventHubDep = Annotated[EventHub, Depends(get_event_hub)]
DatabaseDep = Annotated[Database, Depends(get_db)]
RecPullerDep = Annotated[RecPuller, Depends(get_rec_puller)]
DownloadServiceDep = Annotated[DownloadService, Depends(get_download_service)]

VALID_REC_STATUSES = (
    "in_library",
    "queued",
    "downloaded",
    "search_failed",
    "queue_failed",
    "cancelled",
    "error",
)


# ============================================================================
# Response models
# ============================================================================


class RecsCountsResponse(BaseModel):
    """Configured per-source pull counts (Fresh Picks lives in its own
    `fresh_picks` section of the status response — 2026-08-13)."""

    comfort_zone_count: int
    deep_cuts_count: int


class FreshPicksStatusResponse(BaseModel):
    """The canonical [fresh_picks] section, mirrored into the recs status."""

    pull_window: str
    offset: int
    count: int
    search_buffer: int


class RecsStatusResponse(BaseModel):
    """Response shape for GET /api/recs/status."""

    comfort_zone_enabled: bool
    fresh_picks_enabled: bool
    deep_cuts_enabled: bool
    listenbrainz_enabled: bool
    comfort_zone_interval_days: int
    deep_cuts_interval_days: int
    comfort_zone_playlist_name: str
    fresh_picks_playlist_name: str
    deep_cuts_playlist_name: str
    rotation_trash_rating: int
    counts: RecsCountsResponse
    fresh_picks: FreshPicksStatusResponse
    status_counts: dict[str, int]
    category_warnings: dict[str, str]
    last_pull_at: str | None
    next_pull_at: str | None
    running: bool


class PullResponse(BaseModel):
    """Response shape for POST /api/recs/pull (202)."""

    started: bool


RecCategory = Literal["comfort_zone", "fresh_picks", "deep_cuts"]


class ManualPullRequest(BaseModel):
    """Optional category selection for an explicit manual pull."""

    model_config = ConfigDict(extra="forbid")

    # Omitting categories preserves the original all-category API behavior.
    categories: list[RecCategory] | None = Field(default=None, min_length=1)


class RecsSettingsResponse(BaseModel):
    """Response shape for POST /api/recs/settings."""

    config: dict
    requires_restart: list[str]


class RecRow(BaseModel):
    """A single recommendation row."""

    id: int
    source: str
    artist: str
    track: str
    mbid: str | None
    status: str
    search_id: str | None
    download_id: str | None
    playlist_id: str | None
    created_at: int
    processed_at: int | None


class PendingResponse(BaseModel):
    """Response shape for GET /api/recs/pending."""

    total: int
    items: list[RecRow]


class CancelQueuedResponse(BaseModel):
    """Response shape for POST /api/recs/pending/cancel-queued."""

    cancelled_recs: int
    cancelled_transfers: int
    failed_transfers: int


class AbortResponse(BaseModel):
    """Response shape for POST /api/recs/abort."""

    aborted_pull: bool
    cancelled_recs: int
    cancelled_transfers: int
    failed_transfers: int


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/recs/status", response_model=RecsStatusResponse)
def recs_status(
    config: ConfigDep,
    database: DatabaseDep,
    rec_puller: RecPullerDep,
) -> dict:
    """Return recs settings, status breakdown, last/next pull, and running state."""
    logger.info("GET /api/recs/status")

    data_service = RecsDataService(database)
    status_counts = data_service.status_counts()
    category_warnings = RecsStore(database).category_warnings()

    last_run_at = rec_puller.last_run_at()
    last_pull_at: str | None = None
    if last_run_at is not None:
        last_pull_at = datetime.fromtimestamp(last_run_at, tz=timezone.utc).isoformat()

    # Per-category intervals (P6.5-2) mean there's no single shared cadence
    # any more — next_pull_at is the earliest of the independently-scheduled,
    # enabled categories (comfort_zone, deep_cuts), computed by RecPuller
    # itself since it owns the per-category last-run state. None if neither
    # is enabled (P6.5-3b — no single master switch to check any more).
    next_run_at = rec_puller.next_periodic_pull_at()
    next_pull_at = (
        datetime.fromtimestamp(next_run_at, tz=timezone.utc).isoformat()
        if next_run_at is not None
        else None
    )

    return {
        "comfort_zone_enabled": config.recs.comfort_zone_enabled,
        "fresh_picks_enabled": config.recs.fresh_picks_enabled,
        "deep_cuts_enabled": config.recs.deep_cuts_enabled,
        "listenbrainz_enabled": config.listenbrainz.enabled,
        "comfort_zone_interval_days": config.recs.comfort_zone_interval_days,
        "deep_cuts_interval_days": config.recs.deep_cuts_interval_days,
        "comfort_zone_playlist_name": config.recs.comfort_zone_playlist_name,
        "fresh_picks_playlist_name": config.recs.fresh_picks_playlist_name,
        "deep_cuts_playlist_name": config.recs.deep_cuts_playlist_name,
        "rotation_trash_rating": config.recs.rotation_trash_rating,
        "counts": {
            "comfort_zone_count": config.recs.comfort_zone_count,
            "deep_cuts_count": config.recs.deep_cuts_count,
        },
        "fresh_picks": {
            "pull_window": config.fresh_picks.pull_window,
            "offset": config.fresh_picks.offset,
            "count": config.fresh_picks.count,
            "search_buffer": config.fresh_picks.search_buffer,
        },
        "status_counts": status_counts,
        "category_warnings": category_warnings,
        "last_pull_at": last_pull_at,
        "next_pull_at": next_pull_at,
        "running": rec_puller.is_running(),
    }


@router.post("/recs/pull")
def pull_recs(
    rec_puller: RecPullerDep,
    body: ManualPullRequest | None = None,
) -> JSONResponse:
    """Trigger a manual recommendation pull (RecPuller gates internally)."""
    logger.info("POST /api/recs/pull")

    started = rec_puller.trigger_pull(body.categories if body else None)
    if started:
        return JSONResponse(status_code=202, content={"started": True})

    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "REC_PULL_IN_PROGRESS",
                "message": "A recommendation pull is already running",
                "details": {},
            }
        },
    )


class RecsSettingsUpdateRequest(BaseModel):
    """Body for POST /api/recs/settings — section-shaped like /api/config.

    `recs` and `fresh_picks` are independent sections: Fresh Picks' count
    lives canonically in [fresh_picks] (2026-08-13 — the old
    `recs.fresh_picks_count` alias is gone), so the Recs tab edits it
    directly instead of through a synchronized duplicate.
    """

    model_config = ConfigDict(extra="forbid")

    recs: RecsSettings | None = None
    fresh_picks: FreshPicksSettings | None = None


@router.post("/recs/settings", response_model=RecsSettingsResponse)
def update_recs_settings(
    body: RecsSettingsUpdateRequest,
    config: ConfigDep,
    event_hub: EventHubDep,
) -> dict:
    """Update the [recs] and/or [fresh_picks] sections (hot-applies — both
    are HOT_SECTIONS members)."""
    payload = body.model_dump(exclude_none=True)

    if not payload:
        logger.info("POST /api/recs/settings: no sections provided")
        return {"config": config.to_dict(), "requires_restart": []}

    logger.info(f"POST /api/recs/settings: sections={list(payload)}")

    with open(config.config_path) as f:
        data = toml.load(f)

    for section_name, section_payload in payload.items():
        data.setdefault(section_name, {}).update(section_payload)

    with _config_backup_guard(config):
        with open(config.config_path, "w") as f:
            toml.dump(data, f)
        config.reload()

    event_hub.publish(
        "system.config_reloaded",
        {
            "changed_keys": [
                f"{section}.{key}"
                for section, values in payload.items()
                for key in values
            ]
        },
    )

    return {"config": config.to_dict(), "requires_restart": []}


@router.get("/recs/pending", response_model=PendingResponse)
def get_pending_recs(
    database: DatabaseDep,
    status: str | None = Query(
        None,
        pattern="^(" + "|".join(VALID_REC_STATUSES) + ")$",
    ),
    limit: int = Query(40, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """List recommendation rows, optionally filtered by status."""
    logger.info(f"GET /api/recs/pending: status={status} limit={limit} offset={offset}")

    data_service = RecsDataService(database)
    items, total = data_service.list_recs(status, limit, offset)

    return {"total": total, "items": items}


@router.post("/recs/pending/cancel-queued", response_model=CancelQueuedResponse)
def cancel_queued_recs(
    database: DatabaseDep,
    download_service: DownloadServiceDep,
) -> dict:
    """Cancel every recommendation currently in 'queued' status (see RecsDataService.cancel_all_queued)."""
    logger.info("POST /api/recs/pending/cancel-queued")
    return RecsDataService(database).cancel_all_queued(download_service)


@router.post("/recs/abort", response_model=AbortResponse)
def abort_recs(
    database: DatabaseDep,
    download_service: DownloadServiceDep,
    rec_puller: RecPullerDep,
) -> dict:
    """
    Stop all recommendation activity: request an in-flight pull to stop
    making further ListenBrainz/slskd calls, and cancel every already-
    queued rec download (same as POST /api/recs/pending/cancel-queued).

    A pull's already-dispatched HTTP calls aren't interrupted mid-flight,
    but no further tracks are searched/queued once it notices the abort.
    """
    logger.info("POST /api/recs/abort")
    aborted_pull = rec_puller.request_abort()
    result = RecsDataService(database).cancel_all_queued(download_service)
    return {"aborted_pull": aborted_pull, **result}
