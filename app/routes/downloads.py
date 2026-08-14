"""
Download API routes.

Endpoints:
    GET    /api/transfers                 → List active downloads
    POST   /api/queue                     → Queue downloads (batch; 201 all, 207 partial)
    POST   /api/queue/retry/{transfer_id} → Retry failed download
    DELETE /api/transfers/{id}            → Cancel download
    DELETE /api/transfers?state=finished  → Permanently delete finished transfers

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.config import Config
from app.db.database import Database
from app.db.download_store import DownloadStore
from app.dependencies import (
    get_config,
    get_db_or_none,
    get_download_service,
    get_event_hub,
)
from app.exceptions import InvalidDestinationError
from app.logging_config import get_logger
from app.services.download_data import DownloadDataService
from app.services.interfaces.download import DownloadService, QueueResult, Transfer
from app.sse import EventHub

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["downloads"])

DownloadServiceDep = Annotated[DownloadService, Depends(get_download_service)]
DbDep = Annotated[Database | None, Depends(get_db_or_none)]
EventHubDep = Annotated[EventHub, Depends(get_event_hub)]
ConfigDep = Annotated[Config, Depends(get_config)]


def _validate_destination(destination: str | None, config: Config) -> None:
    """Confine an optional destination override to the configured download dirs.

    Rejects any destination that doesn't resolve to a path under
    paths.download_path, paths.searches_path, or one of the per-category
    discovery trees — guards against path traversal (e.g. "../../etc").
    """
    if not destination:
        return

    resolved = Path(destination).resolve()
    category_roots = [
        config.paths.discovery_familiar_path.resolve(),
        config.paths.discovery_new_releases_path.resolve(),
        config.paths.discovery_exploration_path.resolve(),
    ]
    # Include each derived parent so a caller can still provide a folder such
    # as /music/Discovery/peer1 without introducing a redundant base setting.
    allowed_roots = (
        config.paths.download_path.resolve(),
        config.paths.searches_path.resolve(),
        *category_roots,
        *(root.parent for root in category_roots),
    )
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return

    raise InvalidDestinationError(destination)


FINISHED_STATES = {"completed", "failed", "cancelled"}


# ============================================================================
# Request/Response models
# ============================================================================


class FileRequest(BaseModel):
    """A single file to queue for download."""

    filename: str = Field(..., min_length=1, description="Remote filename on the peer")
    size: int = Field(0, ge=0, description="File size in bytes")

    @field_validator("filename")
    @classmethod
    def _strip_filename(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("filename must not be empty")
        return value


class QueueRequest(BaseModel):
    """Body for POST /api/queue."""

    username: str = Field(..., min_length=1, description="Peer username")
    files: list[FileRequest] = Field(..., min_length=1, description="Files to download")
    search_id: str | None = Field(
        None, description="Search ID that originated these files"
    )
    destination: str | None = Field(None, description="Destination directory override")

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("username must not be empty")
        return value


class QueueResponse(BaseModel):
    """Response shape for POST /api/queue."""

    enqueued_count: int
    failures: list[dict]
    search_id: str | None


class TransferResponse(BaseModel):
    """Response shape for a transfer."""

    transfer_id: str
    username: str
    filename: str
    size: int
    state: str
    progress: float
    speed: int | None
    started_at: str
    completed_at: str | None
    is_rec_download: bool


class RetryResponse(BaseModel):
    """Response shape for POST /api/queue/retry/{id}."""

    success: bool
    message: str
    new_transfer_id: str | None


class CancelResponse(BaseModel):
    """Response shape for DELETE /api/transfers/{id}."""

    transfer_id: str
    cancelled: bool


class DeleteFinishedResponse(BaseModel):
    """Response shape for DELETE /api/transfers?state=finished."""

    deleted_count: int


# ============================================================================
# Serialization helpers
# ============================================================================


def _transfer_to_dict(transfer: Transfer, store: DownloadStore | None) -> dict:
    """Serialize Transfer to dict.

    slskd reports "completed" the instant the network transfer finishes,
    but musica still has to hand the file to beets for tagging/renaming/
    moving — a subprocess that can run 5-20s. Reporting slskd's "completed"
    verbatim tells the UI (and anyone polling this endpoint) the download
    is done and the file is in the library seconds before that is true, and
    for that whole window the file exists at neither the old nor the new
    path. Downgrade to "importing" until musica's own DB confirms beets is
    actually finished with it (moved or explicitly declined).

    Uses `import_pending`, not `import_handled`: a transfer_id musica has no
    downloads row for at all (e.g. slskd reporting history from before a
    reset musica has no memory of) is not "still importing" — it's simply
    not ours to track, and should be left as slskd's own "completed".
    """
    state = transfer.state
    if (
        state == "completed"
        and store is not None
        and store.import_pending(transfer.transfer_id)
    ):
        state = "importing"
    return {
        "transfer_id": transfer.transfer_id,
        "username": transfer.username,
        "filename": transfer.filename,
        "size": transfer.size,
        "state": state,
        "progress": transfer.progress,
        "speed": _normalize_speed(transfer.speed),
        "started_at": _dt_to_iso(transfer.started_at),
        "completed_at": _dt_to_iso(transfer.completed_at),
        "is_rec_download": transfer.is_rec_download,
    }


def _normalize_speed(speed: float | None) -> int | None:
    """slskd reports averageSpeed as float; the API contract is int bytes/sec."""
    if speed is None:
        return None
    return int(speed)


def _dt_to_iso(dt: datetime | None) -> str | None:
    """Serialize datetime (or None) to ISO string."""
    if dt is None:
        return None
    return dt.isoformat() if isinstance(dt, datetime) else str(dt)


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/transfers", response_model=list[TransferResponse])
def list_transfers(
    download_service: DownloadServiceDep,
    db: DbDep,
) -> list[dict]:
    """List active downloads."""
    transfers = download_service.get_status()
    store = DownloadStore(db) if db is not None else None
    return [_transfer_to_dict(t, store) for t in transfers]


@router.post("/queue", response_model=QueueResponse, status_code=201)
def queue_downloads(
    body: QueueRequest,
    download_service: DownloadServiceDep,
    db: DbDep,
    event_hub: EventHubDep,
    config: ConfigDep,
) -> dict | JSONResponse:
    """
    Queue files for download from a peer.

    Returns 201 when all files are enqueued, 207 for partial success.
    """
    _validate_destination(body.destination, config)

    files = [{"filename": f.filename, "size": f.size} for f in body.files]
    logger.info(
        f"POST /api/queue: username='{body.username}', files={len(files)}, "
        f"search_id={body.search_id}"
    )
    result = download_service.queue(
        body.username,
        files,
        search_id=body.search_id,
        destination=body.destination,
    )

    # Persist pending download rows for enqueued files
    if db is not None and result.enqueued_count > 0 and body.search_id:
        is_rec = bool(body.destination and "discovery" in body.destination.lower())
        data_service = DownloadDataService(db)
        persisted = data_service.record_queued_files(
            body.search_id,
            body.username,
            body.files,
            result.failures,
            is_rec,
        )
        logger.info(
            "Persisted %d pending download rows (search_id=%s)",
            persisted,
            body.search_id,
        )

    event_hub.publish(
        "transfer.queued",
        {
            "username": body.username,
            "search_id": body.search_id,
            "enqueued_count": result.enqueued_count,
        },
    )

    if result.enqueued_count < len(files):
        return JSONResponse(status_code=207, content=_queue_to_dict(result))
    return _queue_to_dict(result)


@router.post("/queue/retry/{transfer_id}", response_model=RetryResponse)
def retry_transfer(
    transfer_id: str,
    download_service: DownloadServiceDep,
    event_hub: EventHubDep,
) -> dict:
    """Retry a failed download from stored search results."""
    logger.info(f"POST /api/queue/retry/{transfer_id}")
    result = download_service.retry(transfer_id)
    event_hub.publish(
        "transfer.retried",
        {
            "transfer_id": transfer_id,
            "success": result.success,
            "new_transfer_id": result.new_transfer_id,
        },
    )
    return {
        "success": result.success,
        "message": result.message,
        "new_transfer_id": result.new_transfer_id,
    }


@router.delete("/transfers/{transfer_id}", response_model=CancelResponse)
def cancel_transfer(
    transfer_id: str,
    download_service: DownloadServiceDep,
    event_hub: EventHubDep,
) -> dict:
    """Cancel an active download."""
    logger.info(f"DELETE /api/transfers/{transfer_id}")
    cancelled = download_service.cancel(transfer_id)
    event_hub.publish(
        "transfer.cancelled",
        {"transfer_id": transfer_id, "cancelled": cancelled},
    )
    return {"transfer_id": transfer_id, "cancelled": cancelled}


@router.delete("/transfers", response_model=DeleteFinishedResponse)
def delete_finished_transfers(
    download_service: DownloadServiceDep,
    db: DbDep,
    event_hub: EventHubDep,
    state: Literal["finished"] = Query(..., description="Only 'finished' is supported"),
) -> dict:
    """Permanently delete all completed/failed/cancelled transfers.

    Best-effort on the slskd side (each transfer is asked to be forgotten
    via delete_transfer); the local DB row is removed regardless of the
    slskd-side outcome.
    """
    logger.info(f"DELETE /api/transfers?state={state}")
    transfers = download_service.get_status()
    store = DownloadStore(db) if db is not None else None
    # A "completed" transfer whose file hasn't been handed off to beets yet
    # (see _transfer_to_dict) is not actually finished — deleting its row
    # here would pull the ground out from under DownloadMonitor's
    # mark_file_moved() / mark_import_skipped() calls for it mid-import. Use
    # import_pending (not import_handled) so a transfer musica has no row
    # for at all — slskd reporting history from before a reset — is treated
    # as eligible rather than as permanently "still importing".
    finished_ids = [
        t.transfer_id
        for t in transfers
        if t.state in FINISHED_STATES
        and (
            t.state != "completed"
            or store is None
            or not store.import_pending(t.transfer_id)
        )
    ]

    for transfer_id in finished_ids:
        if not download_service.delete_transfer(transfer_id):
            logger.warning(
                f"slskd-side delete failed for {transfer_id}; removing local row anyway"
            )

    if db is not None and finished_ids:
        DownloadDataService(db).delete_finished(finished_ids)

    event_hub.publish(
        "transfer.deleted",
        {"transfer_ids": finished_ids, "deleted_count": len(finished_ids)},
    )

    logger.info(f"Deleted {len(finished_ids)} finished transfers")
    return {"deleted_count": len(finished_ids)}


def _queue_to_dict(result: QueueResult) -> dict:
    """Serialize QueueResult to dict."""
    return {
        "enqueued_count": result.enqueued_count,
        "failures": result.failures,
        "search_id": result.search_id,
    }
