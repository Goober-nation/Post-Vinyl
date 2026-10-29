"""App factory (#88, #106). Run with:

uvicorn app.main:create_app_from_env --factory
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
from app.routes import auth as auth_routes
from app.routes import catalog as catalog_routes
from app.routes import errors
from app.routes import requests as request_routes
from app.routes import system as system_routes
from app.services.auth import AuthService, AuthServiceBase
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.encryption import SecretBox
from app.services.files import FilesService
from app.services.pipeline import FilePipeline
from app.services.requests import RequestService
from app.services.slskd_cleanup import SlskdCleanup
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_source import SlskdSource
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
    searcher = SlskdSearcher(db, slskd, SlskdGate(slskd, settings.slskd))
    downloader = SlskdDownloader(db, slskd, searcher, settings.slskd)
    dispatch = DownloadDispatch(db, prefs, [SlskdSource(db, searcher, downloader)])
    files = FilesService(db, nd, box, settings.files)
    tagging = TaggingService(
        db, mb, ListenBrainzClient(), CoverArtClient(), box, settings.files
    )
    pipeline = FilePipeline(db, files, tagging, settings.workers)
    cleanup = SlskdCleanup(db, slskd, settings.files)
    workers = CoreLoopWorkers(
        settings.workers, dispatch, pipeline, downloader.poll, cleanup.tick
    )
    return Core(
        CatalogService(db, prefs, catalogs), RequestService(db), dispatch, workers
    )


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
    errors.register(app)
    return app


def create_app_from_env() -> FastAPI:
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
