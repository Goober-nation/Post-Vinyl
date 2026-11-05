"""Pulls: feeder priority, reuse, excluded versions, cancel, playlists."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.clients.catalog import CatalogError, Track
from app.clients.catalog_fake import FakeCatalog
from app.clients.listenbrainz import (
    RadioTrack,
    Recommendations,
    RecommendedRecording,
    RecordingMetadata,
)
from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.clients.navidrome import NavidromeError
from app.clients.navidrome_fake import FakeNavidromeClient
from app.core.pulls import is_excluded
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.download_source_fake import FakeDownloadSource
from app.services.encryption import SecretBox, generate_key
from app.services.playlists import PlaylistService
from app.services.pulls import (
    NoListenBrainzAccount,
    NothingToPull,
    PullNotFound,
    PullRunning,
    PullService,
)
from app.services.requests import RequestService
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.pulls import PullsStore
from app.stores.requests import RequestsStore
from app.stores.users import UsersStore

M = "00000000-0000-4000-8000-00000000000"
TITLES = {1: "One", 2: "Two (live)", 3: "Three", 4: "Four"}


def meta(n: int) -> RecordingMetadata:
    return RecordingMetadata(f"{M}{n}", TITLES[n], 100.0, "Art", (), None, (), (), ())


@dataclass
class Env:
    db: Database
    pulls: PullService
    source: FakeDownloadSource
    lb: FakeListenBrainzClient
    mb: FakeCatalog
    nd: FakeNavidromeClient
    playlists: PlaylistService
    requests: RequestService
    user: int
    list_id: int = 77


@pytest.fixture
def env(tmp_path: Path) -> Env:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    with db.transaction(write=True) as conn:
        users = UsersStore()
        user = users.upsert_on_login(conn, "alice", box.encrypt("pw"), False, 1).id
        users.set_lb_token(conn, user, box.encrypt("tok"), "alice-lb")
    prefs = SourcePreferences(
        db, {"catalog": ("musicbrainz", "listenbrainz"), "download": ("fake",)}
    )
    mb = FakeCatalog("musicbrainz")
    lb = FakeListenBrainzClient()
    lb.tokens["tok"] = "alice-lb"
    for n, title in TITLES.items():
        mb.tracks[f"{M}{n}"] = Track(
            "musicbrainz", f"{M}{n}", title, "Art", (), 100.0, (), ""
        )
        lb.recordings[f"{M}{n}"] = meta(n)
    lb.recs["alice-lb"] = Recommendations(
        77, tuple(RecommendedRecording(f"{M}{n}", 1.0 / n, None) for n in TITLES)
    )
    source = FakeDownloadSource(db)
    requests = RequestService(db)
    dispatch = DownloadDispatch(db, prefs, [source])
    catalog = CatalogService(db, prefs, [mb])
    nd = FakeNavidromeClient({"alice": ("pw", False)})
    return Env(
        db, PullService(db, lb, catalog, requests, dispatch, box, prefs, clock=lambda: 86400),
        source, lb, mb, nd, PlaylistService(db, nd, box), requests, user,
    )  # fmt: skip


def index(env: Env, request_id: int, navidrome_id: str) -> None:
    """The file pipeline's end result for a finished request's song."""
    with env.db.transaction(write=True) as conn:
        req = RequestsStore().get(conn, request_id)
        assert req is not None
        conn.execute(
            """UPDATE file_management SET file_state = 'indexed', navidrome_id = ?
               WHERE song_id = ?""",
            (navidrome_id, req.song_id),
        )


def start_recs(env: Env) -> int:
    assert env.pulls.check_recs(env.user).new
    return env.pulls.pull_recs(env.user, 77, 1, 4, None).id


def test_check_stores_a_generation_once(env: Env) -> None:
    first = env.pulls.check_recs(env.user)
    assert first.new and first.list is not None and first.list.songs == 4
    assert not env.pulls.check_recs(env.user).new
    assert len(env.pulls.rec_lists(env.user)) == 1


def test_no_list_yet_and_no_token(env: Env) -> None:
    env.lb.recs.clear()
    assert env.pulls.check_recs(env.user).list is None
    with env.db.transaction(write=True) as conn:
        UsersStore().set_lb_token(conn, env.user, None)
    with pytest.raises(NoListenBrainzAccount):
        env.pulls.check_recs(env.user)


def test_excluded_versions_stay_out(env: Env) -> None:
    env.pulls.check_recs(env.user)
    titles = [t.title for t in env.pulls.rec_tracks(env.user, 77, 1, 4)]
    assert titles == ["One", "Three", "Four"]
    env.lb.radio_tracks = [RadioTrack(f"{M}2", "Two (live)", "A", None, None)]
    assert env.pulls.radio(env.user, "A", "easy") == []
    with pytest.raises(NothingToPull):
        env.pulls.pull_radio(env.user, "A", "easy", env.lb.radio_tracks, None)


def test_a_dot_in_the_title_doesnt_hide_the_version() -> None:
    assert is_excluded("J.A.R. (Jason Andrew Relva) (demo)")
    assert not is_excluded("Mr. Brightside")


def test_default_name_and_one_running_pull(env: Env) -> None:
    start_recs(env)
    assert env.pulls.pulls(env.user)[0].pull.name == "recommendations-1970-01-02"
    with pytest.raises(PullRunning):
        env.pulls.pull_recs(env.user, 77, 1, 1, None)


def test_feeder_waits_for_every_request_before_a_download(env: Env) -> None:
    start_recs(env)
    env.pulls.feed()
    assert len(env.source.started) == 1
    env.pulls.feed()
    assert len(env.source.started) == 1  # the first is still searching
    user_req = env.requests.create(env.user, 1, "catalog")  # a user's request waits
    env.source.finish(env.source.started[0], "f")
    env.pulls.feed()
    assert len(env.source.started) == 1
    env.requests.drop(env.user, user_req.id)
    env.pulls.feed()
    assert len(env.source.started) == 2


def test_song_in_library_is_reused_without_a_request(env: Env) -> None:
    start_recs(env)
    env.pulls.feed()
    first = env.source.started[0]
    env.source.finish(first, "f")
    index(env, first, "nd-1")
    pull = env.pulls.pulls(env.user)[0]
    env.pulls.cancel(env.user, pull.pull.id)
    second = env.pulls.pull_recs(env.user, 77, 1, 1, "again").id
    env.pulls.feed()
    with env.db.transaction() as conn:
        assert [i.state for i in PullsStore().items(conn, second)] == ["reused"]
    assert len(env.source.started) == 1


def test_unknown_recording_fails_and_the_next_goes(env: Env) -> None:
    del env.mb.tracks[f"{M}1"]
    pull = start_recs(env)
    env.pulls.feed()
    assert len(env.source.started) == 1
    assert env.pulls.pulls(env.user)[0].counts.failed == 1 and pull


def test_cancel_drops_waiting_songs_but_not_a_started_download(env: Env) -> None:
    pull = start_recs(env)
    env.pulls.feed()
    first = env.source.started[0]
    with env.db.transaction(write=True) as conn:
        conn.execute(
            "UPDATE requests SET track_state = 'midDownload' WHERE id = ?", (first,)
        )
    assert env.pulls.cancel(env.user, pull).state == "cancelled"
    assert env.source.cancelled == []
    assert env.requests.get(env.user, first).track_state == "midDownload"
    env.pulls.feed()
    assert len(env.source.started) == 1
    with env.db.transaction() as conn:
        assert [i.state for i in PullsStore().items(conn, pull)] == [
            "requested", "cancelled", "cancelled"]  # fmt: skip


def test_cancel_drops_a_request_still_searching(env: Env) -> None:
    pull = start_recs(env)
    env.pulls.feed()
    env.pulls.cancel(env.user, pull)
    assert env.source.cancelled == env.source.started
    assert env.requests.get(env.user, env.source.started[0]).track_state == "dropped"


def test_cancel_is_owner_only(env: Env) -> None:
    pull = start_recs(env)
    with pytest.raises(PullNotFound):
        env.pulls.cancel(env.user + 1, pull)


def test_pull_ends_and_playlist_follows_indexed_songs(env: Env) -> None:
    pull = start_recs(env)
    done = []
    for n in range(3):
        env.pulls.feed()
        req = env.source.started[n]
        env.source.finish(req, f"f{n}")
        index(env, req, f"nd-{n}")
        done.append(f"nd-{n}")
        env.playlists.sync()
        [(owner, name, songs)] = env.nd.playlists.values()
        assert (owner, name, songs) == ("alice", "recommendations-1970-01-02", done)
    env.pulls.feed()
    view = env.pulls.pulls(env.user)[0]
    assert view.pull.state == "done" and view.counts.in_playlist == 3
    assert view.pull.id == pull


def test_playlist_removed_in_navidrome_comes_back(env: Env) -> None:
    start_recs(env)
    for n in (0, 1):
        env.pulls.feed()
        env.source.finish(env.source.started[n], "f")
        index(env, env.source.started[n], f"nd-{n}")
        env.playlists.sync()
        env.nd.playlists.clear()
    env.pulls.feed()
    env.source.finish(env.source.started[2], "f")
    index(env, env.source.started[2], "nd-2")
    env.playlists.sync()
    assert [p[2] for p in env.nd.playlists.values()] == [["nd-0", "nd-1", "nd-2"]]


def library_pull(env: Env, found: dict[str, str] | Exception) -> int:
    def lookup(title: str, _artist: str) -> str | None:
        if isinstance(found, Exception):
            raise found
        return found.get(title)

    env.pulls._in_library = lookup
    return start_recs(env)


def test_song_already_in_navidrome_isnt_downloaded_and_joins_the_playlist(
    env: Env,
) -> None:
    pull = library_pull(env, {TITLES[1]: "lib-1"})
    env.pulls.feed()
    with env.db.transaction() as conn:
        first = PullsStore().items(conn, pull)[0]
        assert first.state == "reused" and first.request_id is None
        assert PullsStore().indexed_songs(conn, pull) == ["lib-1"]
    env.playlists.sync()
    with env.db.transaction() as conn:
        assert PullsStore().items(conn, pull)[0].in_playlist == 1


def test_no_download_while_the_library_cant_be_checked(env: Env) -> None:
    pull = library_pull(env, NavidromeError("down"))
    env.pulls.feed()
    assert env.source.started == []
    with env.db.transaction() as conn:
        assert PullsStore().items(conn, pull)[0].state == "queued"


def test_a_recs_pull_keeps_its_list_date_and_ranks(env: Env) -> None:
    start_recs(env)
    pull = env.pulls.pulls(env.user)[0].pull
    assert (pull.lb_last_updated, pull.first_rank, pull.last_rank) == (77, 1, 4)


def prefs_of(env: Env) -> SourcePreferences:
    return SourcePreferences(
        env.db, {"catalog": ("musicbrainz", "listenbrainz"), "download": ("fake",)}
    )


def test_catalog_outage_keeps_the_song_queued(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_recs(env)

    def down(track_id: str) -> Track:
        raise CatalogError("down")

    with monkeypatch.context() as m:
        m.setattr(env.mb, "get_track", down)
        env.pulls.feed()
    assert env.source.started == []
    env.pulls.feed()
    assert len(env.source.started) == 1


def test_owner_turning_the_catalog_off_keeps_the_song_queued(env: Env) -> None:
    start_recs(env)
    prefs = prefs_of(env)
    prefs.set_for_user(env.user, "catalog", [], ["musicbrainz"])
    env.pulls.feed()
    assert env.source.started == []
    prefs.set_for_user(env.user, "catalog", [], [])
    env.pulls.feed()
    assert len(env.source.started) == 1


def test_a_user_with_no_download_source_blocks_nobody(env: Env) -> None:
    start_recs(env)
    env.pulls.feed()
    first = env.source.started[0]
    env.source.finish(first, "f")
    with env.db.transaction(write=True) as conn:
        bob = UsersStore().upsert_on_login(conn, "bob", "e", False, 1).id
    prefs_of(env).set_for_user(bob, "download", [], ["fake"])
    env.requests.create(bob, 1, "catalog")
    env.pulls.feed()
    assert len(env.source.started) == 2


def test_a_pull_waits_while_its_owner_has_no_download_source(env: Env) -> None:
    start_recs(env)
    prefs_of(env).set_for_user(env.user, "download", [], ["fake"])
    env.pulls.feed()
    with env.db.transaction() as conn:
        assert PullsStore().users_with_waiting_requests(conn) == []


def test_a_moved_song_goes_into_its_playlists_again(env: Env) -> None:
    start_recs(env)
    env.pulls.feed()
    first = env.source.started[0]
    env.source.finish(first, "f")
    index(env, first, "nd-1")
    env.playlists.sync()
    with env.db.transaction(write=True) as conn:
        assert PullsStore().pulls_to_sync(conn) == []
        req = RequestsStore().get(conn, first)
        assert req is not None
        PullsStore().song_moved(conn, req.song_id, "nd-1", "nd-2")
        conn.execute("UPDATE file_management SET navidrome_id = 'nd-2'")
        assert len(PullsStore().pulls_to_sync(conn)) == 1
