"""FastAPI dependencies shared by routes (#88)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.services.auth import AuthServiceBase
from app.services.catalog import CatalogServiceBase
from app.services.dispatch import DownloadDispatchBase
from app.services.requests import RequestServiceBase
from app.stores.users import User
from app.workers.core_loop import CoreLoopWorkers


@dataclass(frozen=True)
class Core:
    """The services routes call, plus the workers the app runs (#106)."""

    catalog: CatalogServiceBase
    requests: RequestServiceBase
    dispatch: DownloadDispatchBase
    workers: CoreLoopWorkers | None = None


def get_auth(request: Request) -> AuthServiceBase:
    auth: AuthServiceBase = request.app.state.auth
    return auth


def current_user(request: Request) -> User:
    """The logged-in user; the default-deny middleware has already checked it."""
    user: User = request.state.user
    return user


def get_core(request: Request) -> Core:
    core: Core | None = request.app.state.core
    if core is None:  # an app built without the core loop (auth-only tests)
        raise HTTPException(503, "the core services aren't running")
    return core


AuthDep = Annotated[AuthServiceBase, Depends(get_auth)]
UserDep = Annotated[User, Depends(current_user)]
CoreDep = Annotated[Core, Depends(get_core)]
