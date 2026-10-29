"""Settings (#109): the schema, each user's own, the install's (admin).

The frontend draws every field from ``GET /api/settings/schema``. A PUT sends
only the changed keys; all are checked before any is stored. Install settings
apply at the next restart (``restart_pending`` lists what's waiting).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import SchemaField
from app.dependencies import AdminDep, CoreDep, UserDep, need
from app.services.settings import InstallView

router = APIRouter(tags=["settings"])


class Schema(BaseModel):
    user: list[SchemaField]
    install: list[SchemaField]  # only admins can change these


class Changes(BaseModel):
    changes: dict[str, Any]


@router.get("/api/settings/schema")
def schema(_: UserDep, core: CoreDep) -> Schema:
    return Schema(user=need(core.settings, "settings").schema(),
                  install=need(core.install, "install settings").schema())  # fmt: skip


@router.get("/api/me/settings")
def mine(user: UserDep, core: CoreDep) -> dict[str, Any]:
    return need(core.settings, "settings").get(user.id)


@router.put("/api/me/settings")
def set_mine(body: Changes, user: UserDep, core: CoreDep) -> dict[str, Any]:
    return need(core.settings, "settings").set(user.id, body.changes)


@router.get("/api/admin/settings")
def install(_: AdminDep, core: CoreDep) -> InstallView:
    return need(core.install, "install settings").get()


@router.put("/api/admin/settings")
def set_install(body: Changes, _: AdminDep, core: CoreDep) -> InstallView:
    """Written to the config file (comments kept, a backup made); applies at restart."""
    return need(core.install, "install settings").set(body.changes)
