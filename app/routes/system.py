"""Health check (#88), open without a session; service status, for users."""

from __future__ import annotations

from fastapi import APIRouter

from app.dependencies import CoreDep, UserDep, need
from app.services.status import ServiceInfo, ServicePing

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/services")
def services(_: UserDep, core: CoreDep) -> list[ServiceInfo]:
    return need(core.status, "status").services()


@router.get("/services/{name}")
def ping_service(name: str, user: UserDep, core: CoreDep) -> ServicePing:
    return need(core.status, "status").ping(user.id, name)
