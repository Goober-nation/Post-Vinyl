"""Login, logout and the current user (#88)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.dependencies import AuthDep, UserDep
from app.middleware import SESSION_COOKIE
from app.services.auth import (
    SESSION_LIFETIME_S,
    AdminExists,
    InvalidCredentials,
    LoginLocked,
    LoginResult,
    NavidromeUnavailable,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class FirstAdminBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class FirstAdminStatus(BaseModel):
    needed: bool


class Me(BaseModel):
    username: str
    is_admin: bool


def _logged_in(response: Response, result: LoginResult) -> Me:
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=SESSION_LIFETIME_S,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return Me(username=result.user.username, is_admin=bool(result.user.is_admin))


@router.post("/login")
def login(body: LoginBody, response: Response, auth: AuthDep) -> Me:
    try:
        result = auth.login(body.username, body.password)
    except InvalidCredentials as e:
        raise HTTPException(401, str(e)) from e
    except LoginLocked as e:
        raise HTTPException(
            429, str(e), headers={"Retry-After": str(e.retry_after_s)}
        ) from e
    except NavidromeUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return _logged_in(response, result)


@router.get("/first-admin")
def first_admin_status(auth: AuthDep) -> FirstAdminStatus:
    try:
        return FirstAdminStatus(needed=auth.needs_first_admin())
    except NavidromeUnavailable as e:
        raise HTTPException(503, str(e)) from e


@router.post("/first-admin")
def create_first_admin(body: FirstAdminBody, response: Response, auth: AuthDep) -> Me:
    try:
        result = auth.create_first_admin(body.username, body.password)
    except AdminExists as e:
        raise HTTPException(409, str(e)) from e
    except NavidromeUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return _logged_in(response, result)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, auth: AuthDep) -> None:
    auth.logout(request.cookies[SESSION_COOKIE])
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax")


@router.get("/me")
def me(user: UserDep) -> Me:
    return Me(username=user.username, is_admin=bool(user.is_admin))
