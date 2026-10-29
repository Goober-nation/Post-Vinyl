"""slskd tab routes (#108): real services and SQLite, fake slskd, searches inline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.navidrome_fake import FakeNavidromeClient
from app.clients.slskd import PeerFile, PeerResponse
from app.clients.slskd_fake import FakeSlskdClient
from app.config import Settings, SlskdSettings
from app.dependencies import Core
from app.main import create_app
from app.services.auth import AuthService
from app.services.catalog_fake import FakeCatalogService
from app.services.dispatch import DownloadDispatch
from app.services.downloads import DownloadsService
from app.services.encryption import SecretBox, generate_key
from app.services.queue_pause import QueuePause
from app.services.requests import RequestService
from app.services.settings import UserSettings
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_tab import SlskdTab
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.requests import RequestsStore
from app.stores.slskd import SlskdStore
from app.stores.songs import SongFields, SongsStore
from app.stores.users import UsersStore
from tests.test_routes.test_catalog_requests import ORIGIN, SAME, logged_in

TAKK = r"music\Sigur Rós - Takk… (2005)\03. Sigur Rós - Hoppípolla.flac"
LIVE = r"shared\Sigur Rós\Inni (Live)\06. Hoppípolla.flac"
CD3 = r"Music\Sigur Rós\Takk\CD3\03 Hoppípolla.mp3"
LOCKED = r"x\Sigur Rós\Takk\03 Hoppípolla.flac"


def pf(path: str, length: int = 268, locked: bool = False) -> PeerFile:
    ext = path.rsplit(".", 1)[-1]
    return PeerFile(path, 30_000_000, ext, length, None, 16, 44100, locked)


RESULTS = [
    PeerResponse("alice_peer", True, 900, 0, (pf(TAKK), pf(LIVE), pf(LOCKED, locked=True))),
    PeerResponse("cd3_peer", False, 100, 4, (pf(CD3),)),
]  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@dataclass
class Env:
    alice: TestClient
    bob: TestClient
    slskd: FakeSlskdClient
    db: Database
    clock: Clock
    downloader: SlskdDownloader
    pause: QueuePause
    prefs: SourcePreferences


def make_env(tmp_path: Path, slskd: FakeSlskdClient) -> Env:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    nd = FakeNavidromeClient({"alice": ("pw", True), "bob": ("pw", False)})
    auth = AuthService(db, nd, SecretBox(generate_key()))
    clock, cfg = Clock(), SlskdSettings()
    pause = QueuePause(db)
    prefs = SourcePreferences(db, {"download": ("slskd",)})
    gate = SlskdGate(slskd, cfg, clock, clock.sleep, paused=pause.paused)
    searcher = SlskdSearcher(db, slskd, gate, clock, clock.sleep)
    downloader = SlskdDownloader(db, slskd, searcher, cfg, clock)
    tab = SlskdTab(
        db, slskd, gate, searcher, downloader, run_in_background=lambda fn: fn(),
        paused=pause.paused, sources=prefs,
    )  # fmt: skip
    core = Core(
        FakeCatalogService(),
        RequestService(db),
        DownloadDispatch(db, SourcePreferences(db, {}), [], paused=pause.paused),
        tab,
        DownloadsService(db, slskd, downloader, sources=prefs),
        pause,
        settings=UserSettings(db, prefs),
    )
    app = create_app(Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN), auth, core)
    return Env(
        logged_in(app, "alice"), logged_in(app, "bob"), slskd, db, clock, downloader,
        pause, prefs,
    )  # fmt: skip


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, FakeSlskdClient({"hoppipolla": RESULTS}))


def typed(c: TestClient, query: str = "hoppipolla") -> int:
    r = c.post("/api/slskd/searches", json={"query": query}, headers=SAME)
    assert r.status_code == 202 and r.json()["track_state"] == "searching", r.text
    rid: int = r.json()["id"]
    return rid


def test_typed_search_lists_every_file_by_peer_with_exclusions(env: Env) -> None:
    rid = typed(env.alice)
    [listed] = env.alice.get("/api/slskd/searches").json()
    assert listed["request"]["track_state"] == "found"
    assert listed["latest"]["is_complete"] and listed["latest"]["file_count"] == 4
    assert listed["latest"]["peers"] == []  # the list carries no files
    [search] = env.alice.get(f"/api/slskd/searches/{rid}").json()
    assert search["strategy"] == "typed" and search["query"] == "hoppipolla"
    labels = {
        p["username"]: [(f["filename"], f["label"]) for f in p["files"]]
        for p in search["peers"]
    }
    assert labels == {
        "alice_peer": [(TAKK, None), (LIVE, "excluded:live"), (LOCKED, "locked")],
        "cd3_peer": [(CD3, None)],
    }


def test_repeating_a_query_re_reads_and_only_not_found_searches_again(env: Env) -> None:
    rid = typed(env.alice)
    r = env.alice.post(
        "/api/slskd/searches", json={"query": "HOPPIPOLLA"}, headers=SAME
    )
    assert r.json()["id"] == rid and r.json()["track_state"] == "found"
    assert len(env.slskd.searches) == 1  # re-read, not re-searched
    empty = typed(env.alice, "nothing at all")
    assert env.alice.get(f"/api/requests/{empty}").json()["track_state"] == "notFound"
    assert typed(env.alice, "nothing at all") == empty
    assert len(env.slskd.searches) == 3


def test_each_picked_file_is_its_own_request_and_song(env: Env) -> None:
    rid = typed(env.alice)
    files = [{"username": "alice_peer", "filename": TAKK},
             {"username": "cd3_peer", "filename": CD3}]  # fmt: skip
    r = env.alice.post(
        "/api/slskd/downloads", json={"request_id": rid, "files": files}, headers=SAME
    )
    assert r.status_code == 201, r.text
    picks = r.json()
    assert [p["track_state"] for p in picks] == ["midDownload", "midDownload"]
    assert [p["source"] for p in picks] == ["slskd", "slskd"]
    with env.db.transaction() as conn:
        songs = [SongsStore().get(conn, p["song_id"]) for p in picks]
        attempts = [
            SlskdStore().attempts_for_download(conn, d["id"])[0]
            for p in picks
            for d in [conn.execute("SELECT id FROM downloads WHERE request_id = ?",
                                   (p["id"],)).fetchone()]
        ]  # fmt: skip
    assert [(s.title, s.artist_name, s.duration_s) for s in songs if s] == [
        ("Hoppípolla", "Sigur Rós", 268),
        ("Hoppípolla", "Sigur Rós", 268),  # CD3\ path: disc folder skipped
    ]
    assert [(a.username, a.filename) for a in attempts] == [
        ("alice_peer", TAKK),
        ("cd3_peer", CD3),
    ]
    assert (
        len(env.alice.get("/api/slskd/searches").json()) == 1
    )  # picks aren't searches


def test_a_picked_file_that_fails_has_no_fallback(env: Env) -> None:
    rid = typed(env.alice)
    env.slskd.offline_peers.add("alice_peer")
    r = env.alice.post(
        "/api/slskd/downloads",
        json={
            "request_id": rid,
            "files": [{"username": "alice_peer", "filename": TAKK}],
        },
        headers=SAME,
    )
    [pick] = r.json()
    assert pick["track_state"] == "notDownloaded"


def test_picks_are_checked_against_the_search_before_anything_is_written(
    env: Env,
) -> None:
    rid = typed(env.alice)
    good = {"username": "alice_peer", "filename": TAKK}
    for bad in (
        {"username": "alice_peer", "filename": r"made\up.flac"},
        {"username": "cd3_peer", "filename": TAKK},  # right file, wrong peer
        {"username": "alice_peer", "filename": LOCKED},
    ):
        r = env.alice.post("/api/slskd/downloads",
                           json={"request_id": rid, "files": [good, bad]}, headers=SAME)  # fmt: skip
        assert r.status_code == 404, bad
    with env.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 0
    r = env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": []},
                       headers=SAME)  # fmt: skip
    assert r.status_code == 422


def test_other_users_see_and_queue_nothing(env: Env) -> None:
    rid = typed(env.alice)
    assert env.bob.get(f"/api/slskd/searches/{rid}").status_code == 404
    assert env.bob.get("/api/slskd/searches").json() == []
    r = env.bob.post(
        "/api/slskd/downloads",
        json={
            "request_id": rid,
            "files": [{"username": "alice_peer", "filename": TAKK}],
        },
        headers=SAME,
    )
    assert r.status_code == 404


def test_a_song_request_gets_full_matcher_labels(env: Env) -> None:
    with env.db.transaction(write=True) as conn:
        alice = UsersStore().get_by_username(conn, "alice")
        assert alice is not None
        uid = alice.id
        song = SongsStore().create(
            conn, SongFields("Hoppípolla", "Sigur Rós", "Hoppípolla", "Sigur Rós",
                             duration_s=269),
        )  # fmt: skip
        req = RequestsStore().create(conn, uid, "catalog", 1, song_id=song.id)
    sid = env.slskd.start_search("hoppipolla", 600, 30)
    with env.db.transaction(write=True) as conn:
        SlskdStore().add_search(conn, sid, req.id, "hoppipolla", "L0", 1)
    [search] = env.alice.get(f"/api/requests/{req.id}/searches").json()
    labels = {f["filename"]: f["label"] for p in search["peers"] for f in p["files"]}
    assert labels == {
        TAKK: "correct",
        LIVE: "excluded:live",
        LOCKED: "locked",
        CD3: "correct",
    }
    r = env.alice.post(
        "/api/slskd/downloads",
        json={
            "request_id": req.id,
            "files": [{"username": "alice_peer", "filename": TAKK}],
        },
        headers=SAME,
    )
    assert r.status_code == 422  # picking is for typed searches


class LateResponses(FakeSlskdClient):
    """slskd 0.26.2 after a cancel (#110): the first read of the responses is []."""

    def __init__(self) -> None:
        super().__init__({"hoppipolla": RESULTS})
        self.reads = 0

    def get_responses(self, search_id: str) -> list[PeerResponse]:
        self.reads += 1
        return [] if self.reads == 1 else super().get_responses(search_id)


def test_view_is_not_complete_until_slskd_returns_the_responses(tmp_path: Path) -> None:
    env = make_env(tmp_path, LateResponses())
    rid = typed(env.alice)
    [first] = env.alice.get(f"/api/slskd/searches/{rid}").json()
    assert not first["is_complete"] and first["peers"] == []
    [second] = env.alice.get(f"/api/slskd/searches/{rid}").json()
    assert second["is_complete"] and len(second["peers"]) == 2


def test_a_picked_file_waits_in_the_peers_queue_without_a_limit(env: Env) -> None:
    rid = typed(env.alice)
    files = [{"username": "alice_peer", "filename": TAKK}]
    env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": files},
                   headers=SAME)  # fmt: skip
    env.downloader.poll()
    env.clock.t += (
        3600  # an hour in the peer's queue: a catalog download gives up at 15 s
    )
    env.downloader.poll()
    with env.db.transaction() as conn:
        [attempt] = SlskdStore().running_attempts(conn)
    assert attempt.outcome is None and attempt.filename == TAKK


def test_nothing_reaches_slskd_while_it_is_off_for_the_user(env: Env) -> None:
    rid = typed(env.alice)
    alice_dl_files = [{"username": "alice_peer", "filename": TAKK}]
    env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": alice_dl_files},
                   headers=SAME)  # fmt: skip
    dl = env.alice.get("/api/downloads").json()["items"][0]["id"]
    with env.db.transaction() as conn:
        alice = UsersStore().get_by_username(conn, "alice")
    assert alice is not None
    env.prefs.set_for_user(alice.id, "download", [], ["slskd"])
    searches = len(env.slskd.searches)
    r = env.alice.post("/api/slskd/searches", json={"query": "other"}, headers=SAME)
    assert r.status_code == 409 and "slskd is turned off" in r.text
    r = env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": alice_dl_files},
                       headers=SAME)  # fmt: skip
    assert r.status_code == 409
    assert (
        env.alice.post(f"/api/downloads/{dl}/next-peer", headers=SAME).status_code
        == 409
    )
    assert len(env.slskd.searches) == searches  # nothing was sent
    assert (
        env.bob.post(
            "/api/slskd/searches", json={"query": "x"}, headers=SAME
        ).status_code
        == 202
    )
