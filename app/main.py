"""
Musica FastAPI application.

Entry point:
    python -m app.main
or:
    uvicorn app.main:app

Services are wired into app.state by create_app(); routes access them via
dependencies (app/dependencies.py). Tests build isolated apps with fake
services via create_app().
"""

import base64
import binascii
import os
import secrets
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app import APP_NAME, APP_VERSION
from app.config import Config
from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.search_store import SearchStore
from app.exceptions import (
    ConfigError,
    ConfigValidationError,
    InvalidDestinationError,
    MusicaError,
    MusicBrainzNotFoundError,
    MusicBrainzRateLimitError,
    PlaylistNotFoundError,
    SearchNotFoundError,
    SearchRateLimitedError,
    ServiceConnectionError,
    TransferNotFoundError,
)
from app.logging_config import get_logger, setup_logging
from app.routes.config import router as config_router
from app.routes.downloads import router as downloads_router
from app.routes.events import router as events_router
from app.routes.library import router as library_router
from app.routes.musicbrainz import router as musicbrainz_router
from app.routes.recs import router as recs_router
from app.routes.search import router as search_router
from app.routes.setup import router as setup_router
from app.routes.system import router as system_router
from app.services.bootstrap import (
    check_slskd_download_dir,
    ensure_listenbrainz_linked,
    ensure_navidrome_files,
)
from app.services.download import SlskdDownload
from app.services.feedback import ListenBrainzFeedback
from app.services.interfaces.download import DownloadService
from app.services.interfaces.recommendation import RecommendationService
from app.services.interfaces.search import SearchService
from app.services.library import LibraryService
from app.services.musicbrainz_client import MusicBrainzClient
from app.services.navidrome_library import NavidromeLibrary
from app.services.recommendation import ListenBrainzRecs
from app.services.search import SlskdSearch
from app.sse import EventHub
from app.workers.download_monitor import DownloadMonitor
from app.workers.history_cleaner import HistoryCleaner
from app.workers.love_sync import LoveSync
from app.workers.rec_puller import RecPuller
from app.workers.trash_purge import TrashPurge

logger = get_logger(__name__)

# Exceptions that map to HTTP 404
_NOT_FOUND_EXCEPTIONS = (
    SearchNotFoundError,
    TransferNotFoundError,
    PlaylistNotFoundError,
    MusicBrainzNotFoundError,
)


def _musica_error_status(exc: MusicaError) -> int:
    """Map a MusicaError to an HTTP status code."""
    if isinstance(exc, (ConfigValidationError, InvalidDestinationError)):
        return 400
    if isinstance(exc, _NOT_FOUND_EXCEPTIONS):
        return 404
    if isinstance(exc, SearchRateLimitedError):
        return 429
    if isinstance(exc, MusicBrainzRateLimitError):
        return 503
    if isinstance(exc, ServiceConnectionError):
        return 503
    return 500


def create_app(
    config: Config | None = None,
    search_service: SearchService | None = None,
    download_service: DownloadService | None = None,
    library_service: LibraryService | None = None,
    recs_service: RecommendationService | None = None,
    event_hub: EventHub | None = None,
    database: Database | None = None,
) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        config: Config instance (defaults used if None).
        search_service: SearchService implementation (SlskdSearch if None).
        download_service: DownloadService implementation (SlskdDownload if None).
        library_service: LibraryService implementation (NavidromeLibrary if None).
        recs_service: RecommendationService implementation (ListenBrainzRecs if None).
        event_hub: EventHub instance (created if None).

    Returns:
        Configured FastAPI app.
    """
    config_provided = config is not None
    config = config or Config()
    # Database is created up front (cheap — no file I/O until connect()) so
    # the services below can be wired with their SQLite-backed stores (P6.5-4).
    # Only done when a real config (or explicit database) is provided — a
    # bare create_app() with no config is the test path and must not touch
    # the default /app/data paths. Schema initialization still happens in
    # lifespan; the stores only touch the DB lazily after that.
    db = (
        database
        if database is not None
        else (Database(config) if config_provided else None)
    )
    services = {
        "search": search_service
        or SlskdSearch(config, store=SearchStore(db) if db is not None else None),
        "download": download_service
        or SlskdDownload(config, store=DownloadStore(db) if db is not None else None),
        "library": library_service or NavidromeLibrary(config),
        "recs": recs_service or ListenBrainzRecs(config),
        "feedback": ListenBrainzFeedback(config),
        "musicbrainz": MusicBrainzClient(config),
    }

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
    if config.auth.enabled:
        app.add_middleware(BasicAuthMiddleware, config=config)
    app.state.config = config
    app.state.services = services
    app.state.event_hub = event_hub or EventHub()
    app.state.db = db
    app.state.started_at = time.monotonic()
    app.state.listenbrainz_last_check = None

    _register_exception_handlers(app)

    app.include_router(search_router)
    app.include_router(downloads_router)
    app.include_router(library_router)
    app.include_router(config_router)
    app.include_router(events_router)
    app.include_router(system_router)
    app.include_router(recs_router)
    app.include_router(musicbrainz_router)
    app.include_router(setup_router)

    _mount_static(app)
    return app


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth gate for the whole app (single user, credentials from .env).

    Only mounted when config.auth.enabled (both MUSICA_AUTH_USERNAME and
    MUSICA_AUTH_PASSWORD are set) — an explicit opt-in rather than a silent gap.
    """

    def __init__(self, app: FastAPI, config: Config) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/api/system/ping":
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        if not self._is_valid(header):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Musica"'},
            )
        return await call_next(request)

    def _is_valid(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return False
        return secrets.compare_digest(
            username, self._config.auth.username
        ) and secrets.compare_digest(password, self._config.auth.password)


def _mount_static(app: FastAPI) -> None:
    """Serve the frontend (placeholder UI) from app/static if present."""
    static_dir = Path(__file__).parent / "static"
    if (static_dir / "index.html").exists():

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _sanitize_validation_errors(errors: Sequence[Any]) -> list:
    """Convert pydantic error objects to JSON-serializable dicts."""
    cleaned = []
    for err in errors:
        err = dict(err)
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            err["ctx"] = {str(k): str(v) for k, v in ctx.items()}
        cleaned.append(err)
    return cleaned


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Configure structured logging on startup.

    Ensures service logs (app.services.*) are visible under both
    `uvicorn app.main:app` and `python -m app.main`.
    """
    setup_logging(app.state.config)

    # Ensure database is initialized
    db = app.state.db or Database(app.state.config)
    app.state.db = db
    db.initialize_schema()

    # Auto-fill .ndignore / favorites.nsp into music_dir if missing —
    # skippable via paths.ensure_navidrome_files for setups that manage
    # these files themselves (e.g. a hand-edited favorites.nsp or an
    # .ndignore covering more than just download_dir).
    if app.state.config.paths.ensure_navidrome_files:
        try:
            ensure_navidrome_files(app.state.config)
        except Exception:
            logger.warning("ensure_navidrome_files failed", exc_info=True)

    # Warn loudly if slskd's download dir has drifted from what
    # DownloadMonitor expects (e.g. download_dir changed via Config without
    # updating slskd_config/slskd.yml to match)
    try:
        check_slskd_download_dir(app.state.config)
    except Exception:
        logger.warning("check_slskd_download_dir failed", exc_info=True)

    # Re-link Navidrome ListenBrainz scrobbling to whatever token is
    # currently in .env — covers a token that was never saved through the
    # secrets API (hand-edited .env) and self-heals a Navidrome that lost
    # its link (e.g. data volume reset)
    try:
        ensure_listenbrainz_linked(app.state.config)
    except Exception:
        logger.warning("ensure_listenbrainz_linked failed", exc_info=True)

    # Start DownloadMonitor worker
    worker: DownloadMonitor | None = None
    try:
        worker = DownloadMonitor(
            app.state.config,
            app.state.services["download"],
            app.state.services["library"],
            db,
            app.state.event_hub,
        )
        worker.start()
    except Exception:
        logger.warning("Failed to start DownloadMonitor", exc_info=True)

    # Start RecPuller worker
    rec_puller: RecPuller | None = None
    try:
        rec_puller = RecPuller(
            app.state.config,
            app.state.services["recs"],
            app.state.services["library"],
            app.state.services["search"],
            app.state.services["download"],
            db,
            app.state.event_hub,
        )
        app.state.rec_puller = rec_puller
        rec_puller.start()
    except Exception:
        logger.warning("Failed to start RecPuller", exc_info=True)

    # Start the sync workers (P6.7-5 / P6.7-6): once at startup, then every
    # sync.interval_hours.
    love_sync: LoveSync | None = None
    try:
        love_sync = LoveSync(
            app.state.config,
            app.state.services["library"],
            app.state.services["feedback"],
            db,
        )
        app.state.love_sync = love_sync
        love_sync.start()
    except Exception:
        logger.warning("Failed to start LoveSync", exc_info=True)

    trash_purge: TrashPurge | None = None
    try:
        trash_purge = TrashPurge(
            app.state.config,
            app.state.services["library"],
            app.state.services["feedback"],
            db,
        )
        app.state.trash_purge = trash_purge
        trash_purge.start()
    except Exception:
        logger.warning("Failed to start TrashPurge", exc_info=True)

    # P6.9-7: periodically clear slskd's terminal transfer history (the
    # user's live finding: accumulated history congested the stack).
    history_cleaner: HistoryCleaner | None = None
    try:
        history_cleaner = HistoryCleaner(
            app.state.config,
            app.state.services["download"],
            db,
            app.state.event_hub,
        )
        app.state.history_cleaner = history_cleaner
        history_cleaner.start()
    except Exception:
        logger.warning("Failed to start HistoryCleaner", exc_info=True)

    yield

    # Signal SSE streams + both worker threads to stop *before* joining
    # either, so they wind down concurrently in the background. Then join
    # against a single shared 5s deadline (not 5s per worker sequentially)
    # — two sequential 5s joins could add up to ~10s, right at Docker's
    # default stop grace period, risking a SIGKILL instead of a clean exit.
    app.state.event_hub.signal_shutdown()
    if rec_puller is not None:
        rec_puller.request_stop()
    if worker is not None:
        worker.request_stop()
    if love_sync is not None:
        love_sync.request_stop()
    if trash_purge is not None:
        trash_purge.request_stop()
    if history_cleaner is not None:
        history_cleaner.request_stop()

    deadline = time.monotonic() + 5
    if rec_puller is not None:
        rec_puller.join(timeout=max(0.0, deadline - time.monotonic()))
    if worker is not None:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    if love_sync is not None:
        love_sync.join(timeout=max(0.0, deadline - time.monotonic()))
    if trash_purge is not None:
        trash_purge.join(timeout=max(0.0, deadline - time.monotonic()))
    if history_cleaner is not None:
        history_cleaner.join(timeout=max(0.0, deadline - time.monotonic()))


def _register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers using the spec error format."""

    @app.exception_handler(MusicaError)
    async def musica_error_handler(request: Request, exc: MusicaError) -> JSONResponse:
        status = _musica_error_status(exc)
        # 503s here are always a known-external-dependency condition (rate
        # limited, or a downstream service like Navidrome/slskd/MusicBrainz
        # briefly unreachable) that's already been retried with backoff
        # upstream — not a bug in this app. A full traceback for those reads
        # as a crash when it's really just "try again shortly", so only a
        # genuine 500 (unmapped/unexpected) gets the loud treatment.
        if status == 500:
            logger.error(f"MusicaError [{exc.code}]: {exc.message}", exc_info=exc)
        else:
            logger.warning(f"MusicaError [{exc.code}]: {exc.message}")
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning(f"Validation error: {exc.errors()}")
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request body",
                    "details": {"errors": _sanitize_validation_errors(exc.errors())},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": str(exc.detail),
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(f"Unhandled exception: {exc}", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}}
            },
        )


def _load_config() -> Config:
    """Load config.toml + .env, falling back to defaults if unavailable.

    config_path defaults to ./config.toml (relative to cwd) but can be
    overridden via MUSICA_CONFIG_PATH — used in Docker to point at a
    directory-mounted config.toml instead of a single-file bind mount.
    A single-file bind mount can go stale/read-only if the host file is
    replaced via editor atomic-rename (e.g. many editors' "save" behavior);
    mounting the parent directory instead avoids that.
    """
    config_path = os.environ.get("MUSICA_CONFIG_PATH")
    config = Config(config_path=config_path)
    try:
        config.load()
    except ConfigError as e:
        logger.warning(f"Config not loaded, using defaults: {e}")
    return config


# Module-level instance for `uvicorn app.main:app`
app = create_app(_load_config())


def _make_graceful_server(config, event_hub) -> "uvicorn.Server":
    """Build a uvicorn Server whose SIGTERM/SIGINT handling signals
    EventHub shutdown immediately, not on uvicorn's ASGI lifespan
    "shutdown" event.

    uvicorn's Server.shutdown() calls connection.shutdown() on every open
    connection, then waits (up to timeout_graceful_shutdown) for them to
    finish, and only *after* that sends the ASGI lifespan shutdown event
    that normally sets EventHub.shutdown_event — by which point a live SSE
    stream has already been waiting the whole time with no way to know it
    should exit. Overriding handle_exit() (uvicorn's actual signal.signal()
    callback) lets us set shutdown_event the instant the signal arrives, so
    event_stream()'s shutdown_task resolves immediately and the generator
    returns during uvicorn's connection-drain wait — the connection closes
    itself on its own, and the timeout/force-cancel path is never needed.
    timeout_graceful_shutdown stays on as a safety net for anything else
    that doesn't close promptly.
    """
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            event_hub.signal_shutdown()
            super().handle_exit(sig, frame)

    return _Server(config)


def main() -> None:
    """Run the application via `python -m app.main`."""
    config = _load_config()
    application = create_app(config=config)

    import uvicorn

    uvicorn_config = uvicorn.Config(
        application,
        host=config.server.host,
        port=config.server.port,
        timeout_graceful_shutdown=5,
    )
    server = _make_graceful_server(uvicorn_config, application.state.event_hub)
    server.run()


if __name__ == "__main__":
    main()
