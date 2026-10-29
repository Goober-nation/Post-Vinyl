"""Service errors → HTTP status, in one place (#107).

Starlette picks the handler by the error's class hierarchy, so a subclass
(``UnknownSource``, ``IllegalTransition`` are ``ValueError``s) gets its own
status before ``ValueError``'s 422.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.clients.catalog import CatalogError, CatalogNotFound
from app.config import ConfigError
from app.core.states import IllegalTransition
from app.services.catalog import CatalogUnavailable
from app.services.credentials import CredentialCheckUnavailable
from app.services.downloads import AdminOnly, DownloadNotFound, NotRunning
from app.services.files import LibraryFileNotFound, NoAdmin
from app.services.queue_pause import QueuePaused
from app.services.requests import RequestNotFound, SongNotFound, StateChanged
from app.services.slskd_tab import FileNotInSearch
from app.services.sources import SourceOff, UnknownSource
from app.services.tagging import TagFailed

STATUS: dict[type[Exception], int] = {
    ValueError: 422,  # e.g. an empty search
    ConfigError: 422,  # a setting that doesn't fit its schema
    UnknownSource: 404,  # a catalog source the user hasn't enabled
    CatalogNotFound: 404,
    RequestNotFound: 404,
    SongNotFound: 404,
    FileNotInSearch: 404,  # a picked file the search never listed
    IllegalTransition: 409,  # e.g. dropping a request that is already dropped
    StateChanged: 409,
    QueuePaused: 409,  # an admin paused the queue
    NotRunning: 409,  # next peer on a download that isn't running
    DownloadNotFound: 404,
    AdminOnly: 403,
    LibraryFileNotFound: 404,  # no such library song, or its file isn't in the library
    TagFailed: 422,  # e.g. a release that isn't one of the recording's
    CredentialCheckUnavailable: 503,  # ListenBrainz can't check a token now
    NoAdmin: 503,  # no Post-Vinyl admin to reach Navidrome as (setup not run)
    CatalogError: 502,  # the source failed while browsing
    CatalogUnavailable: 503,  # no source answered, or none enabled
    SourceOff: 409,  # the source this needs is turned off
}


def register(app: FastAPI) -> None:
    for error, status in STATUS.items():

        async def handler(
            _: Request, e: Exception, status: int = status
        ) -> JSONResponse:
            return JSONResponse({"detail": str(e)}, status_code=status)

        app.add_exception_handler(error, handler)


def status_for(e: Exception) -> int | None:
    """The status ``register`` would answer for ``e``, the most specific class first."""
    return next((STATUS[c] for c in type(e).__mro__ if c in STATUS), None)
