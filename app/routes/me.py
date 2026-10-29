"""The logged-in user's own credentials (#112): write-only."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies import CoreDep, UserDep, need
from app.services.credentials import CredentialsView

router = APIRouter(prefix="/api/me", tags=["me"])


class Credentials(BaseModel):
    listenbrainz_token: str | None  # null removes it


@router.get("/credentials")
def credentials(user: UserDep, core: CoreDep) -> CredentialsView:
    """Which credentials are stored; their values are never returned."""
    return need(core.credentials, "credentials").view(user.id)


@router.put("/credentials")
def set_credentials(body: Credentials, user: UserDep, core: CoreDep) -> CredentialsView:
    svc = need(core.credentials, "credentials")
    return svc.set_listenbrainz(user.id, body.listenbrainz_token)
