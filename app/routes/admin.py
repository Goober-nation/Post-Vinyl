"""Admin: pausing the queue (#111). More admin routes come with #112."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies import AdminDep, CoreDep, need

router = APIRouter(prefix="/api/admin", tags=["admin"])


class QueueState(BaseModel):
    paused: bool


@router.get("/queue")
def queue_state(_: AdminDep, core: CoreDep) -> QueueState:
    return QueueState(paused=need(core.pause, "the queue pause").paused())


@router.post("/queue/pause")
def pause(_: AdminDep, core: CoreDep) -> QueueState:
    """No new searches, downloads or picks start; running downloads carry on."""
    need(core.pause, "the queue pause").set_paused(True)
    return QueueState(paused=True)


@router.post("/queue/resume")
def resume(_: AdminDep, core: CoreDep) -> QueueState:
    """Waiting requests start on the next redispatch tick (within a minute)."""
    need(core.pause, "the queue pause").set_paused(False)
    return QueueState(paused=False)
