"""The slskd tab (#108): typed searches, their files, queueing picked files.

The tab polls ``GET /api/slskd/searches/{request_id}`` while a search runs;
its state comes live from slskd, the files once slskd has stored them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies import CoreDep, UserDep
from app.routes.requests import RequestOut, out
from app.services.slskd_tab import (
    RECENT_LIMIT,
    Pick,
    SearchView,
    SlskdTabBase,
    TypedSearch,
)

router = APIRouter(prefix="/api/slskd", tags=["slskd"])


class NewSearch(BaseModel):
    query: str


class PickedFile(BaseModel):
    username: str
    filename: str  # the peer's path, exactly as the search listed it


class Picks(BaseModel):
    request_id: int  # the typed search the files were picked from
    files: list[PickedFile]


class TypedSearchOut(BaseModel):
    request: RequestOut
    latest: SearchView | None


def tab(core: CoreDep) -> SlskdTabBase:
    if core.slskd is None:
        raise HTTPException(503, "slskd isn't configured")
    return core.slskd


@router.post("/searches", status_code=202)
def start_search(body: NewSearch, user: UserDep, core: CoreDep) -> RequestOut:
    return out(tab(core).search(user.id, body.query))


@router.get("/searches")
def recent(
    user: UserDep, core: CoreDep, limit: int = RECENT_LIMIT
) -> list[TypedSearchOut]:
    found: list[TypedSearch] = tab(core).recent(user.id, limit)
    return [TypedSearchOut(request=out(t.request), latest=t.latest) for t in found]


@router.get("/searches/{request_id}")
def searches(request_id: int, user: UserDep, core: CoreDep) -> list[SearchView]:
    return tab(core).searches(user.id, request_id)


@router.post("/downloads", status_code=201)
def queue(body: Picks, user: UserDep, core: CoreDep) -> list[RequestOut]:
    picks = [Pick(f.username, f.filename) for f in body.files]
    return [out(r, core) for r in tab(core).queue(user.id, body.request_id, picks)]
