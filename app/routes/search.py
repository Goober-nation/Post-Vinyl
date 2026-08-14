"""
Search API routes.

Endpoints:
    POST /api/search                    → Initiate search (201)
    GET  /api/searches                  → List recent searches
    GET  /api/searches/{search_id}      → Search details + results
    POST /api/searches/{search_id}/cancel → Cancel search

Error responses follow the spec format:
    {"error": {"code": "...", "message": "...", "details": {...}}}
Status codes are handled centrally in app/main.py exception handlers.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.database import Database
from app.db.search_store import SearchStore
from app.dependencies import get_db_or_none, get_event_hub, get_search_service
from app.exceptions import SearchNotFoundError
from app.logging_config import get_logger
from app.services.interfaces.search import SearchJob, SearchResult, SearchService
from app.sse import EventHub

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["search"])

SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
EventHubDep = Annotated[EventHub, Depends(get_event_hub)]
DbDep = Annotated[Database | None, Depends(get_db_or_none)]

RECENT_SEARCHES_LIMIT = 20


# ============================================================================
# Request/Response models
# ============================================================================


class SearchRequest(BaseModel):
    """Body for POST /api/search.

    At least one of query/artist must be non-blank. If query is blank and
    artist is given, the artist value is used as the slskd search query
    directly (artist-only search) and the post-filter is skipped.
    """

    query: str = Field("", description="Track or album name to search for")
    artist: str | None = Field(None, description="Artist name for post-filtering")

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        return value.strip()

    @field_validator("artist")
    @classmethod
    def _strip_artist(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _require_query_or_artist(self) -> "SearchRequest":
        if not self.query and not self.artist:
            raise ValueError("at least one of query or artist must be provided")
        return self


class SearchResponse(BaseModel):
    """Response shape for a search job."""

    search_id: str
    query: str
    artist: str | None
    created_at: str
    status: str


class SearchResultResponse(BaseModel):
    """Response shape for a search result (peer response)."""

    username: str
    filename: str
    size: int
    has_free_slot: bool
    upload_speed: int | None
    bitrate: str | None
    duration: int | None


class SearchDetailResponse(BaseModel):
    """Response shape for GET /api/searches/{id}."""

    search: SearchResponse
    results: list[SearchResultResponse]
    expired: bool = False


class SearchProgressResponse(BaseModel):
    """Response shape for GET /api/searches/{id}/progress."""

    response_count: int
    file_count: int
    is_complete: bool
    elapsed_seconds: float
    threshold: int
    max_wait_seconds: int
    response_cap: int = 250
    stop_reason: str | None = None


# ============================================================================
# Serialization helpers
# ============================================================================


def _job_to_dict(job: SearchJob) -> dict:
    """Serialize SearchJob to dict."""
    return {
        "search_id": job.search_id,
        "query": job.query,
        "artist": job.artist,
        "created_at": job.created_at.isoformat()
        if isinstance(job.created_at, datetime)
        else str(job.created_at),
        "status": job.status,
    }


def _header_row_to_dict(row: dict) -> dict:
    """Serialize a `searches` table row to the SearchResponse shape."""
    return {
        "search_id": row["id"],
        "query": row["query"],
        "artist": row["artist"],
        "created_at": datetime.fromtimestamp(
            row["created_at"], tz=timezone.utc
        ).isoformat(),
        "status": row["status"],
    }


def _result_to_dict(result: SearchResult) -> dict:
    """Serialize SearchResult to dict."""
    return {
        "username": result.username,
        "filename": result.filename,
        "size": result.size,
        "has_free_slot": result.has_free_slot,
        "upload_speed": result.upload_speed,
        "bitrate": result.bitrate,
        "duration": result.duration,
    }


# ============================================================================
# Endpoints
# ============================================================================


@router.post("/search", response_model=SearchResponse, status_code=201)
def create_search(
    body: SearchRequest,
    search_service: SearchServiceDep,
    db: DbDep,
    event_hub: EventHubDep,
) -> dict:
    """
    Initiate a search on slskd.

    Returns 201 with the created search job.
    """
    logger.info(f"POST /api/search: query='{body.query}', artist='{body.artist}'")

    # Artist-only search: use the artist as the slskd query directly and
    # skip the post-filter (job.artist stays None so _filter_by_artist
    # never runs).
    query, artist = body.query, body.artist
    if not query and artist:
        query, artist = artist, None

    job = search_service.search(query, artist=artist)
    logger.info(f"Search initiated: id={job.search_id}")

    # Persist the header only — never the peer responses. slskd already
    # retains those for a period; results are re-fetched via search_id
    # when a saved search is reopened (see get_search below).
    if db is not None:
        SearchStore(db).insert_search(job.search_id, job.query, job.artist, job.status)

    event_hub.publish(
        "search.started",
        {"search_id": job.search_id, "query": job.query, "artist": job.artist},
    )
    return _job_to_dict(job)


@router.get("/searches", response_model=list[SearchResponse])
def list_searches(
    search_service: SearchServiceDep,
    db: DbDep,
) -> list[dict]:
    """List recent searches, newest first, capped at the last 20.

    Backed by the durable `searches` table rather than the search
    service's in-memory job list, so history survives a restart. Falls
    back to the in-memory list when no database is configured.
    """
    if db is not None:
        rows = SearchStore(db).list_recent(RECENT_SEARCHES_LIMIT)
        return [_header_row_to_dict(row) for row in rows]

    jobs = search_service.list_searches()
    return [_job_to_dict(job) for job in jobs[:RECENT_SEARCHES_LIMIT]]


@router.get("/searches/{search_id}", response_model=SearchDetailResponse)
def get_search(
    search_id: str,
    search_service: SearchServiceDep,
    db: DbDep,
    event_hub: EventHubDep,
) -> dict:
    """
    Get search details and results.

    Note: get_results() drives the search to completion (cancel-to-flush),
    so this may block until results are available.

    If the search job has fallen out of the search service's in-memory
    cache (e.g. after a restart) but its header is still in the database,
    slskd itself has very likely also expired the underlying search by
    now — this returns the stored header with an empty result set and
    `expired: true` instead of a bare 404, so the caller can prompt a
    re-search instead of treating it as an unknown id.
    """
    logger.info(f"GET /api/searches/{search_id}")

    try:
        job = search_service.get_status(search_id)
        results = search_service.get_results(search_id)
    except SearchNotFoundError:
        if db is not None:
            row = SearchStore(db).get_search(search_id)
            if row is not None:
                logger.info(f"Search expired from service cache: id={search_id}")
                return {
                    "search": _header_row_to_dict(row),
                    "results": [],
                    "expired": True,
                }
        raise

    if db is not None:
        SearchStore(db).update_status(
            search_id,
            job.status,
            response_count=len({r.username for r in results}),
            file_count=len(results),
        )

    event_hub.publish(
        "search.completed",
        {
            "search_id": search_id,
            "response_count": len({r.username for r in results}),
            "file_count": len(results),
        },
    )
    return {
        "search": _job_to_dict(job),
        "results": [_result_to_dict(result) for result in results],
        "expired": False,
    }


@router.get("/searches/{search_id}/progress", response_model=SearchProgressResponse)
def get_search_progress(
    search_id: str,
    search_service: SearchServiceDep,
) -> dict:
    """
    Peek at a search's live progress (response/file counts, elapsed time)
    without driving it to completion — safe to poll repeatedly while a
    search is still running.
    """
    return search_service.get_progress(search_id)


@router.post("/searches/{search_id}/cancel", response_model=dict)
def cancel_search(
    search_id: str,
    search_service: SearchServiceDep,
    event_hub: EventHubDep,
) -> dict:
    """Cancel an in-progress search."""
    logger.info(f"POST /api/searches/{search_id}/cancel")
    cancelled = search_service.cancel(search_id)
    event_hub.publish(
        "search.cancelled", {"search_id": search_id, "cancelled": cancelled}
    )
    return {"search_id": search_id, "cancelled": cancelled}
