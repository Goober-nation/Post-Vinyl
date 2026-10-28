"""Default deny and the CSRF check (#88).

Every request needs a valid session cookie unless its route is on the
allowlist. State-changing methods also need an ``Origin`` (or, failing that,
``Referer``) matching the app's public origin; a request with neither is
refused (user, 2026-10-28).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.services.auth import AuthServiceBase

SESSION_COOKIE = "pv_session"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OPEN_ROUTES = frozenset({("POST", "/api/auth/login"), ("GET", "/api/system/ping")})
DEV_OPEN_PATHS = frozenset({"/docs", "/openapi.json", "/docs/oauth2-redirect"})


def request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def make_guard(
    auth: AuthServiceBase, public_origin: str, dev: bool
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        method, path = request.method, request.url.path
        if method in UNSAFE_METHODS and request_origin(request) != public_origin:
            return JSONResponse({"detail": "cross-site request refused"}, 403)
        if (method, path) in OPEN_ROUTES or (dev and path in DEV_OPEN_PATHS):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        user = await run_in_threadpool(auth.authenticate, token) if token else None
        if user is None:
            return JSONResponse({"detail": "not logged in"}, 401)
        request.state.user = user
        return await call_next(request)

    return guard
