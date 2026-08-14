"""
FastAPI dependencies for accessing application services.

Services are stored on app.state at startup (see app/main.py:create_app).
Dependencies decouple routes from global singletons and make testing easy:
tests can build an app with fake services via create_app().
"""

from fastapi import Request

from app.config import Config
from app.db.database import Database
from app.exceptions import ServiceConnectionError
from app.services.interfaces.download import DownloadService
from app.services.interfaces.musicbrainz import MusicBrainzService
from app.services.interfaces.search import SearchService
from app.services.library import LibraryService
from app.sse import EventHub
from app.workers.rec_puller import RecPuller


def get_event_hub(request: Request) -> EventHub:
    """Get the EventHub instance from app state."""
    return request.app.state.event_hub


def get_config(request: Request) -> Config:
    """Get the Config instance and pick up external hot-reload changes."""
    config = request.app.state.config
    config.reload_if_changed()
    return config


def get_search_service(request: Request) -> SearchService:
    """Get the SearchService instance from app state."""
    return request.app.state.services["search"]


def get_download_service(request: Request) -> DownloadService:
    """Get the DownloadService instance from app state."""
    return request.app.state.services["download"]


def get_library_service(request: Request) -> LibraryService:
    """Get the LibraryService instance from app state."""
    return request.app.state.services["library"]


def get_musicbrainz_service(request: Request) -> MusicBrainzService:
    """Get the MusicBrainzService instance from app state."""
    return request.app.state.services["musicbrainz"]


def get_db(request: Request) -> Database:
    """Get the Database instance from app state."""
    return request.app.state.db


def get_db_or_none(request: Request) -> Database | None:
    """Get the Database instance from app state, or None if not set."""
    return getattr(request.app.state, "db", None)


def get_rec_puller(request: Request) -> RecPuller:
    """Get the RecPuller worker from app state (503 if not available)."""
    rec_puller = getattr(request.app.state, "rec_puller", None)
    if rec_puller is None:
        raise ServiceConnectionError("RecPuller worker is not available")
    return rec_puller
