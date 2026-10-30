"""App factory (#88, #106). Run with:

uvicorn app.main:create_app_from_env --factory
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from app.clients.catalog import CatalogSource
from app.clients.coverart import CoverArtClient
from app.clients.deezer import DeezerClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.navidrome import NavidromeClient, NavidromeClientBase
from app.clients.slskd import SlskdClient
from app.config import Settings
from app.dependencies import Core
from app.middleware import make_guard
from app.routes import admin as admin_routes
from app.routes import auth as auth_routes
from app.routes import catalog as catalog_routes
from app.routes import downloads as download_routes
from app.routes import errors
from app.routes import library as library_routes
from app.routes import me as me_routes
from app.routes import requests as request_routes
from app.routes import settings as settings_routes
from app.routes import slskd as slskd_routes
from app.routes import system as system_routes
from app.services.admin import AdminService
from app.services.auth import AuthService, AuthServiceBase
from app.services.catalog import CatalogService
from app.services.credentials import CredentialsService
from app.services.dispatch import DownloadDispatch
from app.services.downloads import DownloadsService
from app.services.encryption import SecretBox
from app.services.files import FilesService
from app.services.library import LibraryService
from app.services.pipeline import FilePipeline
from app.services.queue_pause import QueuePause
from app.services.requests import RequestService
from app.services.settings import InstallSettings, UserSettings
from app.services.slskd_cleanup import SlskdCleanup
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_source import SlskdSource
from app.services.slskd_tab import SlskdTab
from app.services.sources import SourcePreferences
from app.services.tagging import TaggingService
from app.stores.database import Database
from app.workers.core_loop import CoreLoopWorkers


def build_core(
    settings: Settings, db: Database, nd: NavidromeClientBase, box: SecretBox
) -> Core:
    """Every client and service, built once; the same graph the live loop test runs."""
    prefs = SourcePreferences(db, settings.sources)
    mb = MusicBrainzClient()
    catalogs: list[CatalogSource] = [mb, DeezerClient()]
    slskd = SlskdClient(settings.slskd_url, settings.slskd_api_key)
    pause = QueuePause(db)
    gate = SlskdGate(slskd, settings.slskd, paused=pause.paused)
    searcher = SlskdSearcher(db, slskd, gate)
    downloader = SlskdDownloader(db, slskd, searcher, settings.slskd)
    dispatch = DownloadDispatch(
        db, prefs, [SlskdSource(db, searcher, downloader)], paused=pause.paused
    )
    files = FilesService(db, nd, box, settings.files)
    lb = ListenBrainzClient()
    tagging = TaggingService(
        db, mb, lb, CoverArtClient(), box, settings.files,
        search=mb, sources=prefs,
    )  # fmt: skip
    pipeline = FilePipeline(db, files, tagging, settings.workers)
    cleanup = SlskdCleanup(db, slskd, settings.files)
    workers = CoreLoopWorkers(
        settings.workers, dispatch, pipeline, downloader.poll, cleanup.tick
    )
    return Core(
        CatalogService(db, prefs, catalogs),
        RequestService(db),
        dispatch,
        SlskdTab(
            db, slskd, gate, searcher, downloader, paused=pause.paused, sources=prefs
        ),
        DownloadsService(db, slskd, downloader, sources=prefs),
        pause,
        LibraryService(db, files, tagging, box),
        AdminService(db),
        CredentialsService(db, lb, box, prefs),
        UserSettings(db, prefs),
        InstallSettings(settings),
        workers,
    )


WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class WebFiles(StaticFiles):
    """The built frontend. index.html is rechecked on every load (its asset names
    change with each build, and a cached one points at files that are gone);
    the hashed /assets/ files can be cached as usual."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings, auth: AuthServiceBase, core: Core | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        workers = core.workers if core else None
        if workers:
            workers.start()
        try:
            yield
        finally:
            if workers:
                workers.stop()

    app = FastAPI(
        title="Post-Vinyl",
        docs_url="/docs" if settings.dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.dev else None,
        lifespan=lifespan,
    )
    app.state.auth = auth
    app.state.core = core
    app.middleware("http")(make_guard(auth, settings.public_origin, settings.dev))
    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    app.include_router(catalog_routes.router)
    app.include_router(request_routes.router)
    app.include_router(slskd_routes.router)
    app.include_router(download_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(library_routes.router)
    app.include_router(me_routes.router)
    app.include_router(settings_routes.router)
    errors.register(app)
    if WEB_DIST.is_dir():  # built by the Dockerfile's web stage or `npm run build`
        app.mount("/", WebFiles(directory=WEB_DIST, html=True), name="web")
    return app


POLLED = ("/api/downloads", "/api/slskd/searches", "/api/requests", "/api/system/ping")


class QuietPolls(logging.Filter):
    """Successful (200) polls of the tabs stay out of the access log (#111, user
    2026-10-30); every other request, and every failed poll, is logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:  # uvicorn's access line
            _, method, path, _, status = args
            if method == "GET" and status == 200 and str(path).startswith(POLLED):
                return False
        return True


def create_app_from_env() -> FastAPI:
    logging.getLogger("uvicorn.access").addFilter(QuietPolls())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    box = SecretBox.from_env()
    db = Database(settings.db_path)
    db.migrate()
    nd = NavidromeClient(settings.navidrome_url)
    auth = AuthService(db, nd, box)
    return create_app(settings, auth, build_core(settings, db, nd, box))
