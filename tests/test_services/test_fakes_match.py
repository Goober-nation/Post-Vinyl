"""The service fakes behave like the real services (#95, #96 review).

Each scenario runs against both; a fake that drifts fails here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.clients.catalog import Album, CatalogBusy, ReleaseRef, Track
from app.clients.catalog_fake import FakeCatalog
from app.clients.listenbrainz import PopularRecording, SimilarArtist
from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.core.states import IllegalTransition
from app.services.catalog import CatalogService, CatalogServiceBase, CatalogUnavailable
from app.services.catalog_fake import FakeCatalogService
from app.services.requests import (
    RequestNotFound,
    RequestService,
    RequestServiceBase,
    SongNotFound,
)
from app.services.requests_fake import FakeRequestService
from app.services.sources import SourceOff, SourcePreferences, UnknownSource
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> float:
        self.t += 1
        return self.t


# ------------------------------------------------------------ requests


@dataclass
class Req:
    svc: RequestServiceBase
    alice: int
    bob: int
    song: int
    alice_origin: int
    bob_origin: int


@pytest.fixture(params=["real", "fake"])
def req(request: pytest.FixtureRequest, tmp_path: Path) -> Req:
    if request.param == "fake":
        fake = FakeRequestService()
        fake.songs.add(1)
        fake.origins.update({10: 1, 11: 2})
        return Req(fake, 1, 2, 1, 10, 11)
    db = Database(tmp_path / "pv.db")
    db.migrate()
    store = RequestsStore()
    with db.transaction() as conn:
        a = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
        b = UsersStore().upsert_on_login(conn, "bob", "e", False, NOW).id
        song = SongsStore().create(conn, SongFields("Feather", "Nujabes")).id
        ao = store.record_catalog_search(conn, a, "q", "track", NOW).id
        bo = store.record_catalog_search(conn, b, "q", "track", NOW).id
    return Req(RequestService(db, Clock()), a, b, song, ao, bo)


def test_requests_create_rules(req: Req) -> None:
    s = req.svc
    assert s.create(req.alice, req.song, "catalog", req.alice_origin).track_state == (
        "requested"
    )
    assert (
        s.create(req.alice, req.song, "catalog").id
        != s.create(req.alice, req.song, "catalog").id
    )
    for bad in ("slskd", "other"):
        with pytest.raises(ValueError):
            s.create(req.alice, req.song, bad)
    with pytest.raises(SongNotFound):
        s.create(req.alice, 999, "catalog")
    with pytest.raises(RequestNotFound):
        s.create(req.alice, req.song, "catalog", req.bob_origin)


def test_requests_states_and_ownership(req: Req) -> None:
    s = req.svc
    r = s.create(req.alice, req.song, "catalog")
    with pytest.raises(IllegalTransition):
        s.re_request(req.alice, r.id)
    with pytest.raises(RequestNotFound):
        s.drop(req.bob, r.id)
    assert s.drop(req.alice, r.id).track_state == "dropped"
    assert s.re_request(req.alice, r.id).track_state == "reRequested"


def test_requests_history(req: Req) -> None:
    s = req.svc
    ids = [s.create(req.alice, req.song, "catalog").id for _ in range(3)]
    page = s.history(req.alice, page=1, page_size=2)
    assert len(page.items) == 2 and page.total == len(ids)
    assert s.history(req.alice).page_size == 20
    assert s.history(req.alice, page=0).page == 1
    assert s.history(req.alice, page_size=500).page_size == 100
    assert s.history(req.alice, page=10**30).items == []
    assert s.history(req.bob).total == 0


# ------------------------------------------------------------ catalog


def track(source: str, id: str) -> Track:
    return Track(source, id, "Feather", "Nujabes", (), 175.6, (), "")


class Failing(FakeCatalog):
    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        raise CatalogBusy("rate limited")


@dataclass
class Cat:
    svc: CatalogServiceBase
    user: int
    mb: FakeCatalog
    dz: FakeCatalog
    set_enabled: Callable[[list[str]], None]
    swap: Callable[[str, FakeCatalog], None]


@pytest.fixture(params=["real", "fake"])
def cat(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Cat]:
    mb, dz = FakeCatalog("musicbrainz"), FakeCatalog("deezer")
    names = ("musicbrainz", "deezer")
    if request.param == "fake":
        fake = FakeCatalogService()
        fake.catalogs = {"musicbrainz": mb, "deezer": dz}
        fake.enabled = list(names)

        def set_fake(on: list[str]) -> None:
            fake.enabled = on

        def swap_fake(name: str, c: FakeCatalog) -> None:
            fake.catalogs[name] = c

        yield Cat(fake, 1, mb, dz, set_fake, swap_fake)
        return
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, NOW).id
    prefs = SourcePreferences(db, {"catalog": names})
    real = CatalogService(db, prefs, [mb, dz], Clock())

    def set_real(on: list[str]) -> None:
        prefs.set_for_user(uid, "catalog", on, [n for n in names if n not in on])

    def swap_real(name: str, c: FakeCatalog) -> None:
        real._catalogs[name] = c

    yield Cat(real, uid, mb, dz, set_real, swap_real)


def test_catalog_first_answer_and_origins(cat: Cat) -> None:
    cat.dz.tracks["d1"] = track("deezer", "d1")
    a = cat.svc.search_tracks(cat.user, "Feather")
    assert a.source == "deezer"
    cat.mb.tracks["m1"] = track("musicbrainz", "m1")
    b = cat.svc.search_tracks(cat.user, "feather")
    assert b.source == "musicbrainz" and b.origin_id == a.origin_id
    assert cat.svc.search_tracks(cat.user, "other").origin_id != a.origin_id
    assert cat.svc.search_tracks(cat.user, "zzz").source is None


def test_catalog_errors(cat: Cat) -> None:
    cat.dz.tracks["d1"] = track("deezer", "d1")
    cat.swap("musicbrainz", Failing("musicbrainz"))
    assert cat.svc.search_tracks(cat.user, "Feather").source == "deezer"
    cat.swap("deezer", Failing("deezer"))
    with pytest.raises(CatalogUnavailable, match="answered"):
        cat.svc.search_tracks(cat.user, "Feather")
    cat.set_enabled([])
    with pytest.raises(SourceOff, match="unavailable without sources"):
        cat.svc.search_tracks(cat.user, "Feather")
    with pytest.raises(SourceOff, match="musicbrainz is turned off"):
        cat.svc.edition_tracks(cat.user, "musicbrainz", "a1")


def test_catalog_resolve_and_browse(cat: Cat) -> None:
    cat.mb.tracks["m1"] = track("musicbrainz", "m1")
    song = cat.svc.resolve(cat.user, "musicbrainz", "m1")
    assert (song.title, song.duration_s) == ("Feather", 176)
    del cat.mb.tracks["m1"]
    assert cat.svc.resolve(cat.user, "musicbrainz", "m1").id == song.id
    cat.dz.tracks["d1"] = track("deezer", "d1")
    cat.set_enabled(["musicbrainz"])
    with pytest.raises(SourceOff):
        cat.svc.resolve(cat.user, "deezer", "d1")
    with pytest.raises(SourceOff):
        cat.svc.edition_tracks(cat.user, "deezer", "e1")
    with pytest.raises(UnknownSource):
        cat.svc.edition_tracks(cat.user, "nope", "e1")


def test_catalog_query_checks_and_order(cat: Cat) -> None:
    cat.mb.tracks["boot"] = Track(
        "musicbrainz", "boot", "Feather (N0ms Bootleg)", "Nujabes", (), 206.0, (), "",
        score=100, releases=(ReleaseRef("Official", "Album", ("Compilation",)),),
    )  # fmt: skip
    cat.mb.tracks["studio"] = Track(
        "musicbrainz", "studio", "Feather", "Nujabes", (), 175.0, (), "",
        score=77, releases=(ReleaseRef("Official", "Album", ()),),
    )  # fmt: skip
    res = cat.svc.search_tracks(cat.user, "  Feather ", "  Nujabes  ")
    assert [t.id for t in res.items] == ["studio", "boot"]
    assert (
        cat.svc.search_tracks(cat.user, "Feather", "Nujabes").origin_id == res.origin_id
    )
    for call in (
        lambda: cat.svc.search_tracks(cat.user, "   "),
        lambda: cat.svc.search_albums(cat.user, ""),
        lambda: cat.svc.search_artists(cat.user, " \t "),
    ):
        with pytest.raises(ValueError):
            call()


def test_catalog_resolve_stores_normalized_names(cat: Cat) -> None:
    cat.mb.tracks["m9"] = Track(
        "musicbrainz", "m9", "Feather", "Nujabes featuring Cise Starr & Akin from CYNE",
        (), 175.0, (), "",
        credits=(("Nujabes", " featuring "), ("Cise Starr", " & "), ("Akin from CYNE", "")),
    )  # fmt: skip
    song = cat.svc.resolve(cat.user, "musicbrainz", "m9")
    assert song.artist_name == "Nujabes featuring Cise Starr & Akin from CYNE"
    assert (song.query_title, song.main_artist) == ("Feather", "Nujabes")


def test_musicbrainz_artists_get_listenbrainz_data_and_albums_sort_newest_first(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "lb.db")
    db.migrate()
    with db.transaction(write=True) as conn:
        user = UsersStore().upsert_on_login(conn, "a", "enc", False, 1).id
    mb = FakeCatalog("musicbrainz")
    mb.albums = {
        "old": Album("musicbrainz", "old", "Old", "A", "Album", (), "2001"),
        "new": Album("musicbrainz", "new", "New", "A", "Album", (), "2020-05"),
    }
    mb.albums_by_artist["a1"] = ["old", "new"]
    lb = FakeListenBrainzClient()
    lb.popular["a1"] = [PopularRecording("r1", "Song", "A", 10.0, "rel")]
    lb.similar["a1"] = [SimilarArtist("a2", "B", "")]
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz", "listenbrainz")})
    svc = CatalogService(db, prefs, [mb], listenbrainz=lb)
    assert [a.id for a in svc.artist_albums(user, "musicbrainz", "a1")] == [
        "new",
        "old",
    ]
    [track] = svc.artist_top_tracks(user, "musicbrainz", "a1")
    assert (track.source, track.id, track.cover_id) == (
        "musicbrainz",
        "r1",
        "release/rel",
    )
    [similar] = svc.similar_artists(user, "musicbrainz", "a1")
    assert (similar.source, similar.id) == ("musicbrainz", "a2")


def test_musicbrainz_artists_stay_off_listenbrainz_while_it_is_off(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "off.db")
    db.migrate()
    with db.transaction(write=True) as conn:
        user = UsersStore().upsert_on_login(conn, "a", "e", False, 1).id
    mb = FakeCatalog("musicbrainz")
    lb = FakeListenBrainzClient()
    lb.popular["a1"] = [PopularRecording("r1", "Song", "A", 10.0, "rel")]
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz", "listenbrainz")})
    prefs.set_for_user(user, "catalog", [], ["listenbrainz"])
    svc = CatalogService(db, prefs, [mb], listenbrainz=lb)
    assert svc.artist_top_tracks(user, "musicbrainz", "a1") == []
