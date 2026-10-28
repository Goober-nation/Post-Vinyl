"""Health check (#88); open without a session."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}
