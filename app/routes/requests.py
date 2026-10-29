"""Requests: ask for a picked track, follow it, drop or re-request it (#107).

A new or re-requested request is handed to the download source at once
rather than on the next redispatch tick. A user only sees their own requests.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import Core, CoreDep, UserDep
from app.routes.errors import status_for
from app.services.dispatch import NotStartable
from app.services.requests import DEFAULT_PAGE_SIZE
from app.services.slskd_tab import SearchView
from app.stores.requests import Request

router = APIRouter(prefix="/api/requests", tags=["requests"])
MAX_CART = 100


class NewRequest(BaseModel):
    source: str  # the catalog source the track came from, e.g. "musicbrainz"
    track_id: str
    origin_id: int | None = None  # the search it was picked from (Results.origin_id)


class RequestOut(BaseModel):
    id: int
    song_id: int | None
    source: str
    query: str | None
    track_state: str
    created_at: int
    last_used_at: int
    download_source: str | None = None
    download_state: str | None = None


class RequestPage(BaseModel):
    items: list[RequestOut]
    total: int
    page: int
    page_size: int


def out(req: Request, core: Core | None = None) -> RequestOut:
    """The request, with its download status when ``core`` is given."""
    o = RequestOut(
        id=req.id, song_id=req.song_id, source=req.source, query=req.query,
        track_state=req.track_state, created_at=req.created_at,
        last_used_at=req.last_used_at,
    )  # fmt: skip
    if core is None:
        return o
    st = core.dispatch.status(req.user_id, req.id)
    return o.model_copy(
        update={
            "track_state": st.track_state,
            "download_source": st.source,
            "download_state": st.download_state,
        }
    )


def start(core: Core, req: Request) -> None:
    try:
        core.dispatch.dispatch(req.user_id, req.id)
    except NotStartable:
        pass  # a worker started it meanwhile


@router.post("", status_code=201)
def create(body: NewRequest, user: UserDep, core: CoreDep) -> RequestOut:
    song = core.catalog.resolve(user.id, body.source, body.track_id)
    req = core.requests.create(user.id, song.id, "catalog", body.origin_id)
    start(core, req)
    return out(req, core)


class CartItem(BaseModel):
    source: str
    track_id: str
    origin_id: int | None = None


class Cart(BaseModel):
    items: list[CartItem] = Field(min_length=1, max_length=MAX_CART)


class CartResult(BaseModel):
    source: str
    track_id: str
    request: RequestOut | None = None
    status: int  # 201, or the status this item alone would have got
    error: str | None = None


@router.post("/batch", status_code=207)
def batch(body: Cart, user: UserDep, core: CoreDep) -> list[CartResult]:
    """The browser's cart in one call (#111): each item like ``POST /api/requests``,
    each with its own result, so one bad item doesn't stop the rest."""
    out_items = []
    for item in body.items:
        try:
            r = create(NewRequest(**item.model_dump()), user, core)
            out_items.append(CartResult(source=item.source, track_id=item.track_id,
                                        request=r, status=201))  # fmt: skip
        except Exception as e:
            status = status_for(e)
            if status is None:
                raise
            out_items.append(CartResult(source=item.source, track_id=item.track_id,
                                        status=status, error=str(e)))  # fmt: skip
    return out_items


@router.get("")
def history(
    user: UserDep, core: CoreDep, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
) -> RequestPage:
    p = core.requests.history(user.id, page, page_size)
    return RequestPage(
        items=[out(r) for r in p.items],
        total=p.total,
        page=p.page,
        page_size=p.page_size,
    )


@router.get("/{request_id}")
def get(request_id: int, user: UserDep, core: CoreDep) -> RequestOut:
    return out(core.requests.get(user.id, request_id), core)


@router.get("/{request_id}/searches")
def searches(request_id: int, user: UserDep, core: CoreDep) -> list[SearchView]:
    """The request's slskd searches, files labelled by the matcher."""
    if core.slskd is None:
        raise HTTPException(503, "slskd isn't configured")
    return core.slskd.searches(user.id, request_id)


@router.post("/{request_id}/drop")
def drop(request_id: int, user: UserDep, core: CoreDep) -> RequestOut:
    req = core.requests.drop(user.id, request_id)
    core.dispatch.cancel(user.id, request_id)
    return out(req, core)


@router.post("/{request_id}/re-request")
def re_request(request_id: int, user: UserDep, core: CoreDep) -> RequestOut:
    req = core.requests.re_request(user.id, request_id)
    start(core, req)
    return out(req, core)
