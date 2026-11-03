"""Pull routes: validation at the boundary, ownership, errors as statuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.listenbrainz import Recommendations, RecommendedRecording
from app.clients.listenbrainz_fake import FakeListenBrainzClient
from app.clients.navidrome_fake import FakeNavidromeClient
from app.config import Settings
from app.dependencies import Core
from app.main import create_app
from app.services.auth import AuthService
from app.services.catalog import CatalogService
from app.services.credentials import CredentialsService
from app.services.dispatch import DownloadDispatch
from app.services.download_source_fake import FakeDownloadSource
from app.services.encryption import SecretBox, generate_key
from app.services.pulls import PullService
from app.services.requests import RequestService
from app.services.sources import SourcePreferences
from app.stores.database import Database
from tests.test_routes.test_catalog_requests import ORIGIN, SAME, logged_in

MBID = "00000000-0000-4000-8000-000000000001"
TRACK = {"mbid": MBID, "title": "Song", "artist": "Art"}


@pytest.fixture
def clients(tmp_path: Path) -> tuple[TestClient, TestClient, CredentialsService]:
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    nd = FakeNavidromeClient({"alice": ("pw", False), "bob": ("pw", False)})
    prefs = SourcePreferences(db, {"catalog": ("listenbrainz",), "download": ("fake",)})
    lb = FakeListenBrainzClient()
    lb.tokens["tok"] = "alice-lb"
    lb.recs["alice-lb"] = Recommendations(7, (RecommendedRecording(MBID, 1.0, None),))
    requests = RequestService(db)
    dispatch = DownloadDispatch(db, prefs, [FakeDownloadSource(db)])
    catalog = CatalogService(db, prefs, [])
    core = Core(
        catalog, requests, dispatch,
        pulls=PullService(db, lb, catalog, requests, dispatch, box, prefs),
    )  # fmt: skip
    app = create_app(
        Settings(str(tmp_path / "pv.db"), "http://nd", ORIGIN),
        AuthService(db, nd, box),
        core,
    )
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    return alice, bob, CredentialsService(db, lb, box, prefs)


def test_recs_need_a_token_then_a_list_shows(
    clients: tuple[TestClient, TestClient, CredentialsService],
) -> None:
    alice, _, credentials = clients
    assert alice.post("/api/lb/recs/check", headers=SAME).status_code == 409
    credentials.set_listenbrainz(1, "tok")
    body = alice.post("/api/lb/recs/check", headers=SAME).json()
    assert body["new"] is True and body["list"]["songs"] == 1
    assert alice.get("/api/lb/recs").json()[0]["last_updated"] == 7


def test_radio_pull_validates_and_only_its_owner_cancels(
    clients: tuple[TestClient, TestClient, CredentialsService],
) -> None:
    alice, bob, _ = clients
    url = "/api/pulls/radio"
    bad = {"artist": "A", "level": "normal", "tracks": [TRACK]}
    assert alice.post(url, json=bad, headers=SAME).status_code == 422
    bad = {"artist": "A", "level": "easy", "tracks": [{**TRACK, "mbid": "x"}]}
    assert alice.post(url, json=bad, headers=SAME).status_code == 422
    good = {"artist": "A", "level": "medium", "tracks": [TRACK]}
    pull = alice.post(url, json=good, headers=SAME).json()
    assert (pull["name"].startswith("radio-A-"), pull["total"]) == (True, 1)
    assert alice.post(url, json=good, headers=SAME).status_code == 409
    cancel = f"/api/pulls/{pull['id']}/cancel"
    assert bob.post(cancel, headers=SAME).status_code == 404
    assert alice.post(cancel, headers=SAME).json()["state"] == "cancelled"
    assert alice.post(cancel, headers=SAME).status_code == 409
    assert bob.get("/api/pulls").json() == []


def test_unknown_list_is_404(
    clients: tuple[TestClient, TestClient, CredentialsService],
) -> None:
    assert clients[0].get("/api/lb/recs/9/tracks").status_code == 404
