"""Service errors → HTTP status, in one place (#107).

Starlette picks the handler by the error's class hierarchy, so a subclass
(``UnknownSource``, ``IllegalTransition`` are ``ValueError``s) gets its own
status before ``ValueError``'s 422.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.clients.catalog import CatalogError, CatalogNotFound
from app.core.states import IllegalTransition
from app.services.catalog import CatalogUnavailable
from app.services.requests import RequestNotFound, SongNotFound, StateChanged
from app.services.sources import UnknownSource

STATUS: dict[type[Exception], int] = {
    ValueError: 422,  # e.g. an empty search
    UnknownSource: 404,  # a catalog source the user hasn't enabled
    CatalogNotFound: 404,
    RequestNotFound: 404,
    SongNotFound: 404,
    IllegalTransition: 409,  # e.g. dropping a request that is already dropped
    StateChanged: 409,
    CatalogError: 502,  # the source failed while browsing
    CatalogUnavailable: 503,  # no source answered, or none enabled
}


def register(app: FastAPI) -> None:
    for error, status in STATUS.items():

        async def handler(
            _: Request, e: Exception, status: int = status
        ) -> JSONResponse:
            return JSONResponse({"detail": str(e)}, status_code=status)

        app.add_exception_handler(error, handler)
