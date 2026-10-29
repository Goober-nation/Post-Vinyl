"""Catalog and request routes (#107): real services and SQLite, fake clients."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.catalog import CatalogBusy, Track
from app.clients.catalog_fake import FakeCatalog
from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import Settings
from app.dependencies import Core
from app.main import create_app
from app.services.auth import AuthService
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.download_source_fake import FakeDownloadSource
from app.services.encryption import SecretBox, generate_key
from app.services.requests import RequestService
from app.services.sources import SourcePreferences
from app.stores.database import Database

ORIGIN = "https://testserver"
SAME = {"Origin": ORIGIN}
FEATHER = Track("musicbrainz", "mb-1", "Feather", "Nujabes", (), 175.6, (), "")


@dataclass
class Env:
    alice: TestClient
    bob: TestClient
    mb: FakeCatalog
    source: FakeDownloadSource


class Failing(FakeCatalog):
    def search_tracks(
        self, title: str, artist: str | None = None, limit: int = 25
    ) -> list[Track]:
        raise CatalogBusy("rate limited")


def logged_in(app: FastAPI, user: str) -> TestClient:
    client = TestClient(app, base_url=ORIGIN)
    r = client.post(
        "/api/auth/login", json={"username": user, "password": "pw"}, headers=SAME
    )
    assert r.status_code == 200, r.text
    return client


def make_env(tmp_path: Path, catalogs: list[FakeCatalog]) -> Env:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    nd = FakeNavidromeClient({"alice": ("pw", True), "bob": ("pw", False)})
    auth = AuthService(db, nd, SecretBox(generate_key()))
    prefs = SourcePreferences(
        db, {"catalog": tuple(c.source for c in catalogs), "download": ("fake",)}
    )
    source = FakeDownloadSource(db)
    core = Core(
        CatalogService(db, prefs, list(catalogs)),
        RequestService(db),
        DownloadDispatch(db, prefs, [source]),
    )
    app = create_app(Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN), auth, core)
    return Env(logged_in(app, "alice"), logged_in(app, "bob"), catalogs[0], source)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    mb = FakeCatalog("musicbrainz")
    mb.tracks[FEATHER.id] = FEATHER
    return make_env(tmp_path, [mb])


def request_feather(client: TestClient) -> dict[str, object]:
    found = client.get("/api/catalog/tracks", params={"title": "feather"}).json()
    assert found["source"] == "musicbrainz" and found["items"][0]["id"] == "mb-1"
    r = client.post(
        "/api/requests",
        json={
            "source": "musicbrainz",
            "track_id": "mb-1",
            "origin_id": found["origin_id"],
        },
        headers=SAME,
    )
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


def test_pick_creates_a_request_and_dispatches_it_at_once(env: Env) -> None:
    req = request_feather(env.alice)
    assert req["track_state"] == "searching" and req["source"] == "catalog"
    assert env.source.started == [req["id"]]
    env.source.finish(int(str(req["id"])), "library/Nujabes/Feather.mp3")
    got = env.alice.get(f"/api/requests/{req['id']}").json()
    assert got["download_state"] == "completed"
    assert "file_location" not in got  # paths are slskd's and Navidrome's, not ours


def test_drop_while_searching_cancels_and_re_request_dispatches_again(env: Env) -> None:
    rid = request_feather(env.alice)["id"]
    r = env.alice.post(f"/api/requests/{rid}/drop", headers=SAME)
    assert r.status_code == 200 and r.json()["track_state"] == "dropped"
    assert env.source.cancelled == [rid]
    assert env.alice.post(f"/api/requests/{rid}/drop", headers=SAME).status_code == 409
    r = env.alice.post(f"/api/requests/{rid}/re-request", headers=SAME)
    assert r.json()["track_state"] == "searching"
    assert env.source.started == [rid, rid]


def test_users_only_see_their_own_requests(env: Env) -> None:
    rid = request_feather(env.alice)["id"]
    assert env.bob.get(f"/api/requests/{rid}").status_code == 404
    assert env.bob.post(f"/api/requests/{rid}/drop", headers=SAME).status_code == 404
    assert env.bob.get("/api/requests").json()["total"] == 0
    page = env.alice.get("/api/requests", params={"page_size": 1}).json()
    assert page["total"] == 1 and page["items"][0]["id"] == rid


def test_errors_map_to_status(env: Env) -> None:
    c = env.alice
    assert c.get("/api/catalog/tracks", params={"title": "  "}).status_code == 422
    assert c.get("/api/catalog/tracks").status_code == 422  # title missing
    assert c.get("/api/catalog/deezer/albums/x/editions").status_code == 404
    bad = {"source": "musicbrainz", "track_id": "nope"}
    assert c.post("/api/requests", json=bad, headers=SAME).status_code == 404
    assert c.get("/api/requests/999").status_code == 404
    found = c.get("/api/catalog/tracks", params={"title": "nothing"}).json()
    assert found["source"] is None and found["items"] == []  # empty is not an error


def test_every_source_failing_is_503(tmp_path: Path) -> None:
    env = make_env(tmp_path, [Failing("musicbrainz")])
    r = env.alice.get("/api/catalog/tracks", params={"title": "feather"})
    assert r.status_code == 503


def test_catalog_and_requests_need_a_session(env: Env, tmp_path: Path) -> None:
    anon = TestClient(env.alice.app, base_url=ORIGIN)
    assert anon.get("/api/catalog/tracks", params={"title": "x"}).status_code == 401
    assert anon.get("/api/requests").status_code == 401


def test_cart_batch_gives_each_item_its_own_result(env: Env) -> None:
    found = env.alice.get("/api/catalog/tracks", params={"title": "feather"}).json()
    items = [
        {"source": "musicbrainz", "track_id": "mb-1", "origin_id": found["origin_id"]},
        {"source": "musicbrainz", "track_id": "nope"},
        {"source": "deezer", "track_id": "mb-1"},  # a source the user hasn't enabled
    ]
    r = env.alice.post("/api/requests/batch", json={"items": items}, headers=SAME)
    assert r.status_code == 207
    assert [(x["status"], x["request"] is not None) for x in r.json()] == [
        (201, True), (404, False), (404, False),
    ]  # fmt: skip
    assert env.source.started == [r.json()[0]["request"]["id"]]
    big = {"items": [items[0]] * 101}
    assert (
        env.alice.post("/api/requests/batch", json=big, headers=SAME).status_code == 422
    )
    empty = {"items": []}
    assert (
        env.alice.post("/api/requests/batch", json=empty, headers=SAME).status_code
        == 422
    )
