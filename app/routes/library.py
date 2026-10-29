"""File management (#112): the whole library; rename and delete are admin-only.

A rename is two calls: ``rename`` looks the edited names up and answers the
suggested tag set (or no match); then ``apply`` writes that set, or ``strip``
takes every tag off and moves the file by the new names.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.dependencies import AdminDep, CoreDep, UserDep, need
from app.services.library import LibraryFile, LibraryPage, Rename
from app.services.requests import DEFAULT_PAGE_SIZE

router = APIRouter(prefix="/api/library/files", tags=["library"])


class RenameBody(BaseModel):
    title: str
    artist: str
    album_artist: str = ""
    album: str = ""
    year: str = ""
    track: int | None = None
    disc: int | None = None


class SuggestionOut(BaseModel):
    match: bool
    recording_id: str | None = None
    release_id: str | None = None
    via: str | None = None  # "listenbrainz" | "musicbrainz"
    tags: dict[str, Any] | None = None
    has_cover: bool = False


class Apply(BaseModel):
    recording_id: str
    release_id: str


class Strip(BaseModel):
    title: str
    artist: str


class Moved(BaseModel):
    file: LibraryFile | None  # None: Navidrome doesn't list the new path yet


@router.get("")
def files(
    _: UserDep, core: CoreDep, q: str = "", page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> LibraryPage:  # fmt: skip
    return need(core.library, "the library").page(q, page, page_size)


@router.post("/{song_id}/rename")
def rename(
    song_id: str, body: RenameBody, admin: AdminDep, core: CoreDep
) -> SuggestionOut:
    s = need(core.library, "the library").suggest(
        admin, song_id, Rename(**body.model_dump())
    )
    if s is None:
        return SuggestionOut(match=False)
    return SuggestionOut(match=True, recording_id=s.recording_id, release_id=s.release_id,
                         via=s.via, tags=s.tags, has_cover=s.has_cover)  # fmt: skip


@router.post("/{song_id}/apply")
def apply(song_id: str, body: Apply, admin: AdminDep, core: CoreDep) -> Moved:
    lib = need(core.library, "the library")
    return Moved(file=lib.apply(admin, song_id, body.recording_id, body.release_id))


@router.post("/{song_id}/strip")
def strip(song_id: str, body: Strip, admin: AdminDep, core: CoreDep) -> Moved:
    lib = need(core.library, "the library")
    return Moved(file=lib.strip(admin, song_id, body.title, body.artist))


@router.delete("/{song_id}", status_code=204)
def delete(song_id: str, _: AdminDep, core: CoreDep) -> None:
    need(core.library, "the library").delete(song_id)
