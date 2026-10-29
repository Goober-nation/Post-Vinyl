"""The downloads tab (#111): the queue with live progress, one download, next peer.

The tab polls ``GET /api/downloads`` while open; successful polls stay out of
the access log (``main.py``).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.dependencies import CoreDep, UserDep, need
from app.services.downloads import DownloadDetail, QueuePage
from app.services.requests import DEFAULT_PAGE_SIZE

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


@router.get("")
def queue(
    user: UserDep,
    core: CoreDep,
    state: str | None = None,
    everyone: bool = False,  # admins: every user's downloads
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> QueuePage:
    svc = need(core.downloads, "downloads")
    return svc.page(user.id, bool(user.is_admin), everyone=everyone, state=state,
                    page=page, page_size=page_size)  # fmt: skip


@router.get("/{download_id}")
def get(download_id: int, user: UserDep, core: CoreDep) -> DownloadDetail:
    return need(core.downloads, "downloads").get(
        user.id, bool(user.is_admin), download_id
    )


@router.post("/{download_id}/next-peer")
def next_peer(download_id: int, user: UserDep, core: CoreDep) -> DownloadDetail:
    """Skip the running peer (not counted against it) and queue the next one."""
    svc = need(core.downloads, "downloads")
    return svc.next_peer(user.id, bool(user.is_admin), download_id)
