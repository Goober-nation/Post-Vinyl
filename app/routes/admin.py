"""Admin: pausing the queue (#111), bad peers and the user list (#112)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies import AdminDep, CoreDep, need
from app.services.admin import UserView
from app.stores.slskd import SlskdPeer

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


class Cleared(BaseModel):
    cleared: int


@router.get("/peers")
def peers(_: AdminDep, core: CoreDep) -> list[SlskdPeer]:
    """Peers with a bad record; ``blocked_at`` set = banned."""
    return need(core.admin, "admin").peers()


@router.delete("/peers/{username}")
def clear_peer(username: str, _: AdminDep, core: CoreDep) -> Cleared:
    return Cleared(cleared=int(need(core.admin, "admin").clear_peer(username)))


@router.delete("/peers")
def clear_peers(_: AdminDep, core: CoreDep) -> Cleared:
    return Cleared(cleared=need(core.admin, "admin").clear_peers())


@router.get("/users")
def users(_: AdminDep, core: CoreDep) -> list[UserView]:
    return need(core.admin, "admin").users()
