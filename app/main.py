"""App factory (#88). Run with:

uvicorn app.main:create_app_from_env --factory
"""

from __future__ import annotations

from fastapi import FastAPI

from app.clients.navidrome import NavidromeClient
from app.config import Settings
from app.middleware import make_guard
from app.routes import auth as auth_routes
from app.routes import system as system_routes
from app.services.auth import AuthService, AuthServiceBase
from app.services.encryption import SecretBox
from app.stores.database import Database


def create_app(settings: Settings, auth: AuthServiceBase) -> FastAPI:
    app = FastAPI(
        title="Post-Vinyl",
        docs_url="/docs" if settings.dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.dev else None,
    )
    app.state.auth = auth
    app.middleware("http")(make_guard(auth, settings.public_origin, settings.dev))
    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    return app


def create_app_from_env() -> FastAPI:
    settings = Settings.from_env()
    box = SecretBox.from_env()
    db = Database(settings.db_path)
    db.migrate()
    auth = AuthService(db, NavidromeClient(settings.navidrome_url), box)
    return create_app(settings, auth)
