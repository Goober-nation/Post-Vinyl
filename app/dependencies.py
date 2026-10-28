"""FastAPI dependencies shared by routes (#88)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.services.auth import AuthServiceBase
from app.stores.users import User


def get_auth(request: Request) -> AuthServiceBase:
    auth: AuthServiceBase = request.app.state.auth
    return auth


def current_user(request: Request) -> User:
    """The logged-in user; the default-deny middleware has already checked it."""
    user: User = request.state.user
    return user


AuthDep = Annotated[AuthServiceBase, Depends(get_auth)]
UserDep = Annotated[User, Depends(current_user)]
