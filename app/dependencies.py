"""FastAPI dependencies shared by routes (#88)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, TypeVar

from fastapi import Depends, HTTPException, Request

from app.services.admin import AdminServiceBase
from app.services.auth import AuthServiceBase
from app.services.catalog import CatalogServiceBase
from app.services.credentials import CredentialsServiceBase
from app.services.dispatch import DownloadDispatchBase
from app.services.downloads import DownloadsServiceBase
from app.services.library import LibraryServiceBase
from app.services.queue_pause import QueuePauseBase
from app.services.requests import RequestServiceBase
from app.services.slskd_tab import SlskdTabBase
from app.stores.users import User
from app.workers.core_loop import CoreLoopWorkers

T = TypeVar("T")


@dataclass(frozen=True)
class Core:
    """The services routes call, plus the workers the app runs (#106)."""

    catalog: CatalogServiceBase
    requests: RequestServiceBase
    dispatch: DownloadDispatchBase
    slskd: SlskdTabBase | None = None  # None: slskd isn't configured (tests)
    downloads: DownloadsServiceBase | None = None
    pause: QueuePauseBase | None = None
    library: LibraryServiceBase | None = None
    admin: AdminServiceBase | None = None
    credentials: CredentialsServiceBase | None = None
    workers: CoreLoopWorkers | None = None


def need(service: T | None, what: str) -> T:
    """A service this app was built without (tests) → 503."""
    if service is None:
        raise HTTPException(503, f"{what} isn't configured")
    return service


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


def admin_user(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(403, "admins only")
    return user


AuthDep = Annotated[AuthServiceBase, Depends(get_auth)]
UserDep = Annotated[User, Depends(current_user)]
AdminDep = Annotated[User, Depends(admin_user)]
CoreDep = Annotated[Core, Depends(get_core)]
