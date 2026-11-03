"""ListenBrainz tab: recs lists, radio previews, pulls and cancelling them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.clients.listenbrainz import RadioTrack
from app.dependencies import CoreDep, UserDep, need
from app.services.pulls import PullServiceBase, PullView
from app.stores.pulls import LbList, LbTrack

router = APIRouter(tags=["pulls"])
Level = Literal["easy", "medium", "hard"]
MAX_RANK = 1000
MAX_RADIO_TRACKS = 200
UUID = r"^[0-9a-fA-F-]{36}$"


def pulls(core: CoreDep) -> PullServiceBase:
    return need(core.pulls, "pulls")


class RecCheckOut(BaseModel):
    new: bool
    list: LbList | None


class RadioTrackIn(BaseModel):
    mbid: str = Field(pattern=UUID)
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=500)
    album: str | None = None
    length_s: float | None = None


class NewRecsPull(BaseModel):
    last_updated: int
    first_rank: int = Field(ge=1, le=MAX_RANK)
    last_rank: int = Field(ge=1, le=MAX_RANK)
    name: str | None = Field(default=None, max_length=200)


class NewRadioPull(BaseModel):
    artist: str = Field(min_length=1, max_length=200)
    level: Level
    tracks: list[RadioTrackIn] = Field(min_length=1, max_length=MAX_RADIO_TRACKS)
    name: str | None = Field(default=None, max_length=200)


class PullOut(BaseModel):
    id: int
    kind: str
    name: str
    state: str
    created_at: int
    total: int
    queued: int
    downloading: int
    in_playlist: int
    failed: int


def out(view: PullView) -> PullOut:
    p, c = view.pull, view.counts
    return PullOut(
        id=p.id, kind=p.kind, name=p.name, state=p.state, created_at=p.created_at,
        total=c.total, queued=c.queued, downloading=c.downloading,
        in_playlist=c.in_playlist, failed=c.failed,
    )  # fmt: skip


def view_of(service: PullServiceBase, user_id: int, pull_id: int) -> PullOut:
    return next(out(v) for v in service.pulls(user_id) if v.pull.id == pull_id)


@router.post("/api/lb/recs/check")
def check(user: UserDep, core: CoreDep) -> RecCheckOut:
    result = pulls(core).check_recs(user.id)
    return RecCheckOut(new=result.new, list=result.list)


@router.get("/api/lb/recs")
def lists(user: UserDep, core: CoreDep) -> list[LbList]:
    return pulls(core).rec_lists(user.id)


@router.get("/api/lb/recs/{last_updated}/tracks")
def tracks(
    last_updated: int,
    user: UserDep,
    core: CoreDep,
    first: int = Query(1, ge=1, le=MAX_RANK),
    last: int = Query(MAX_RANK, ge=1, le=MAX_RANK),
) -> list[LbTrack]:
    return pulls(core).rec_tracks(user.id, last_updated, first, last)


@router.get("/api/lb/radio")
def radio(
    user: UserDep,
    core: CoreDep,
    artist: str = Query(min_length=1, max_length=200),
    level: Level = "easy",
) -> list[RadioTrack]:
    return pulls(core).radio(user.id, artist.strip(), level)


@router.post("/api/pulls/recs", status_code=201)
def pull_recs(body: NewRecsPull, user: UserDep, core: CoreDep) -> PullOut:
    service = pulls(core)
    pull = service.pull_recs(
        user.id, body.last_updated, body.first_rank, body.last_rank, body.name
    )
    return view_of(service, user.id, pull.id)


@router.post("/api/pulls/radio", status_code=201)
def pull_radio(body: NewRadioPull, user: UserDep, core: CoreDep) -> PullOut:
    service = pulls(core)
    chosen = [RadioTrack(t.mbid, t.title, t.artist, t.album, t.length_s)
              for t in body.tracks]  # fmt: skip
    pull = service.pull_radio(user.id, body.artist, body.level, chosen, body.name)
    return view_of(service, user.id, pull.id)


@router.get("/api/pulls")
def history(user: UserDep, core: CoreDep) -> list[PullOut]:
    return [out(v) for v in pulls(core).pulls(user.id)]


@router.post("/api/pulls/{pull_id}/cancel")
def cancel(pull_id: int, user: UserDep, core: CoreDep) -> PullOut:
    service = pulls(core)
    service.cancel(user.id, pull_id)
    return view_of(service, user.id, pull_id)
