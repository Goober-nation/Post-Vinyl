"""Catalog service, source preferences and the sources config (#95)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from app.clients.catalog import CatalogBusy, CatalogError, Track
from app.clients.catalog_fake import FakeCatalog
from app.config import ConfigError, load_sources
from app.services.catalog import CatalogService, CatalogUnavailable
from app.services.sources import SourcePreferences, UnknownSource
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore

NOW = 1_800_000_000
CONFIGURED = {"catalog": ("musicbrainz", "deezer", "listenbrainz")}


def track(source: str, id: str, title: str = "Feather") -> Track:
    return Track(source, id, title, "Nujabes", (), 175.6, ("JPB600000001",), "")


class Failing(FakeCatalog):
    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        raise CatalogBusy("rate limited")


Setup = tuple[
    CatalogService, SourcePreferences, Database, int, FakeCatalog, FakeCatalog
]


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "enc", False, NOW).id
    mb, dz = FakeCatalog("musicbrainz"), FakeCatalog("deezer")
    prefs = SourcePreferences(db, CONFIGURED, lambda: NOW)
    return CatalogService(db, prefs, [mb, dz], lambda: NOW), prefs, db, uid, mb, dz


# ------------------------------------------------------------ preferences


def test_user_order_and_off_apply_and_new_config_sources_are_on(setup: Setup) -> None:
    _, prefs, _, uid, _, _ = setup
    assert prefs.for_user(uid, "catalog") == ["musicbrainz", "deezer", "listenbrainz"]
    prefs.set_for_user(uid, "catalog", ["deezer"], ["listenbrainz"])
    # musicbrainz wasn't placed by the user: still on, after their order
    assert prefs.for_user(uid, "catalog") == ["deezer", "musicbrainz"]


def test_user_cannot_add_a_source_the_config_lacks(setup: Setup) -> None:
    _, prefs, _, uid, _, _ = setup
    with pytest.raises(UnknownSource):
        prefs.set_for_user(uid, "catalog", ["spotify"], [])


def test_source_removed_from_config_drops_out_of_user_order(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "enc", False, NOW).id
    SourcePreferences(db, CONFIGURED).set_for_user(
        uid, "catalog", ["deezer", "musicbrainz"], []
    )
    narrowed = SourcePreferences(db, {"catalog": ("musicbrainz",)})
    assert narrowed.for_user(uid, "catalog") == ["musicbrainz"]


# ------------------------------------------------------------ search


def test_first_source_with_results_answers_and_search_is_recorded(
    setup: Setup,
) -> None:
    svc, _, _, uid, mb, dz = setup
    dz.tracks["3135556"] = track("deezer", "3135556")
    res = svc.search_tracks(uid, "Feather", "Nujabes")  # musicbrainz empty → deezer
    assert res.source == "deezer" and [t.id for t in res.items] == ["3135556"]
    mb.tracks["m1"] = track("musicbrainz", "m1")
    again = svc.search_tracks(uid, "feather", "nujabes")
    assert again.source == "musicbrainz"
    assert again.origin_id == res.origin_id  # same typed search, one history row


def test_failing_source_falls_through(setup: Setup) -> None:
    svc, _, _, uid, _, dz = setup
    dz.tracks["1"] = track("deezer", "1")
    svc._catalogs["musicbrainz"] = Failing("musicbrainz")
    assert svc.search_tracks(uid, "Feather").source == "deezer"


def test_all_sources_failing_raises_but_all_empty_is_no_result(setup: Setup) -> None:
    svc, prefs, _, uid, _, _ = setup
    assert svc.search_tracks(uid, "nothing").source is None
    svc._catalogs = {"musicbrainz": Failing("musicbrainz")}
    with pytest.raises(CatalogUnavailable):
        svc.search_tracks(uid, "Feather")
    prefs.set_for_user(uid, "catalog", [], ["musicbrainz", "deezer"])
    with pytest.raises(CatalogUnavailable, match="without sources"):
        svc.search_tracks(uid, "Feather")


def test_order_follows_user_preference(setup: Setup) -> None:
    svc, prefs, _, uid, mb, dz = setup
    mb.tracks["m1"] = track("musicbrainz", "m1")
    dz.tracks["d1"] = track("deezer", "d1")
    prefs.set_for_user(uid, "catalog", ["deezer"], [])
    assert svc.search_tracks(uid, "Feather").source == "deezer"


def test_no_sources_is_a_soft_error_and_writes_no_history(setup: Setup) -> None:
    svc, prefs, db, uid, _, _ = setup
    prefs.set_for_user(uid, "catalog", [], ["musicbrainz", "deezer", "listenbrainz"])
    with pytest.raises(CatalogUnavailable, match="unavailable without sources"):
        svc.search_tracks(uid, "Feather")
    with db.transaction() as conn:
        assert RequestsStore().catalog_history(conn, uid, 1, 10) == []


def test_listenbrainz_only_config_cannot_search(tmp_path: Path) -> None:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "enc", False, NOW).id
    prefs = SourcePreferences(db, {"catalog": ("listenbrainz",)})
    svc = CatalogService(db, prefs, [FakeCatalog("musicbrainz")])
    with pytest.raises(CatalogUnavailable, match="unavailable without sources"):
        svc.search_artists(uid, "Nujabes")


@pytest.mark.parametrize(
    "stored", [["deezer"], "deezer", {"order": "deezer"}, {"order": [], "off": "md"}]
)
def test_malformed_stored_preference_means_defaults(
    setup: Setup, stored: object
) -> None:
    _, prefs, db, uid, _, _ = setup
    with db.transaction() as conn:
        UsersStore().set_setting(conn, uid, "sources.catalog", stored, NOW)
    assert prefs.for_user(uid, "catalog") == list(CONFIGURED["catalog"])


def test_concurrent_resolve_of_a_new_track_gives_one_song(setup: Setup) -> None:
    svc, _, db, uid, mb, _ = setup
    mb.tracks["m1"] = track("musicbrainz", "m1")
    out: list[object] = []
    with db.transaction(write=True) as conn:  # another resolve is mid-write
        SongsStore().find_or_create(
            conn, SongFields("Feather", "Nujabes"), {"musicbrainz": "m1"}
        )
        worker = threading.Thread(
            target=lambda: out.append(
                _try(lambda: svc.resolve(uid, "musicbrainz", "m1"))
            )
        )
        worker.start()
        time.sleep(0.3)
    worker.join(5)
    assert not isinstance(out[0], Exception), out[0]
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1


def _try(call: Callable[[], object]) -> object:
    try:
        return call()
    except sqlite3.Error as e:  # what an unguarded race raises
        return e


# ------------------------------------------------------------ resolve


def test_resolve_stores_only_the_pick_and_reuses_it(setup: Setup) -> None:
    svc, _, db, uid, mb, _ = setup
    mb.tracks["m1"] = track("musicbrainz", "m1")
    song = svc.resolve(uid, "musicbrainz", "m1")
    assert (song.title, song.artist_name, song.duration_s) == (
        "Feather",
        "Nujabes",
        176,
    )
    with db.transaction() as conn:
        assert SongsStore().identities(conn, song.id) == {"musicbrainz": "m1"}
    del mb.tracks["m1"]  # a known pick needs no catalog call
    assert svc.resolve(uid, "musicbrainz", "m1").id == song.id


def test_resolve_and_browse_refuse_a_disabled_source(setup: Setup) -> None:
    svc, prefs, _, uid, _, dz = setup
    dz.tracks["d1"] = track("deezer", "d1")
    prefs.set_for_user(uid, "catalog", [], ["deezer"])
    with pytest.raises(UnknownSource):
        svc.resolve(uid, "deezer", "d1")
    with pytest.raises(UnknownSource):
        svc.edition_tracks(uid, "deezer", "e1")


def test_resolve_passes_catalog_errors_up(setup: Setup) -> None:
    svc, _, _, uid, _, _ = setup
    with pytest.raises(CatalogError):
        svc.resolve(uid, "musicbrainz", "missing")


# ------------------------------------------------------------ config


def test_example_config_loads() -> None:
    sources = load_sources(Path("config/post-vinyl.example.yaml"))
    assert sources["catalog"] == ("musicbrainz", "deezer", "listenbrainz")
    assert sources["request"] == ()


@pytest.mark.parametrize(
    "text, error",
    [
        ("sources:\n  catalog: [spotify]\n", "unknown \\['spotify'\\]"),
        ("sources:\n  catalog: [deezer, deezer]\n", "twice"),
        ("sources:\n  catalog: deezer\n", "list of names"),
        ("sources:\n  lyrics: []\n", "unknown source kinds"),
        ("other: 1\n", "missing 'sources'"),
        ("sources: [\n", "can't read"),
    ],
)
def test_bad_config_refuses(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "pv.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=error):
        load_sources(path)


def test_left_out_kind_is_all_off(tmp_path: Path) -> None:
    path = tmp_path / "pv.yaml"
    path.write_text("sources:\n  catalog: [deezer]\n")
    assert load_sources(path) == {"catalog": ("deezer",), "download": (), "request": ()}
