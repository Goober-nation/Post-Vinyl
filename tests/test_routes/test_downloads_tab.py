"""Downloads tab and queue pause routes (#111): real services and SQLite, fake slskd."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.clients.slskd_fake import FakeSlskdClient
from app.main import QuietPolls
from app.services.queue_pause import QueuePause
from app.stores.slskd import SlskdStore
from tests.test_routes.test_catalog_requests import SAME
from tests.test_routes.test_slskd_tab import CD3, RESULTS, TAKK, Env, make_env, typed


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, FakeSlskdClient({"hoppipolla": RESULTS}))


def pick(c: TestClient, files: list[tuple[str, str]]) -> list[int]:
    rid = typed(c)
    r = c.post(
        "/api/slskd/downloads",
        json={
            "request_id": rid,
            "files": [{"username": u, "filename": f} for u, f in files],
        },
        headers=SAME,
    )
    assert r.status_code == 201, r.text
    return [p["id"] for p in r.json()]


def test_queue_shows_own_downloads_with_live_progress(env: Env) -> None:
    pick(env.alice, [("alice_peer", TAKK)])
    tid = next(iter(env.slskd.transfers))
    env.slskd.transfers[tid] = replace(
        env.slskd.transfers[tid], state="InProgress", bytes_transferred=12_000_000,
        average_speed=400_000, place_in_queue=None,
    )  # fmt: skip
    page = env.alice.get("/api/downloads").json()
    assert page["total"] == 1
    [item] = page["items"]
    assert (item["title"], item["artist"], item["username"]) == (
        "Hoppípolla",
        "Sigur Rós",
        "alice",
    )
    assert item["state"] == "queued" and item["track_state"] == "midDownload"
    assert item["current"]["username"] == "alice_peer" and item["attempts"] == 1
    assert item["progress"] == {
        "state": "InProgress", "bytes_transferred": 12_000_000, "size": 30_000_000,
        "average_speed": 400_000, "place_in_queue": None,
    }  # fmt: skip
    detail = env.alice.get(f"/api/downloads/{item['id']}").json()
    assert [a["username"] for a in detail["attempts"]] == ["alice_peer"]


def test_users_see_their_own_and_only_admins_see_everyone(env: Env) -> None:
    pick(env.alice, [("alice_peer", TAKK)])
    pick(env.bob, [("cd3_peer", CD3)])
    assert [i["username"] for i in env.bob.get("/api/downloads").json()["items"]] == [
        "bob"
    ]
    assert env.bob.get("/api/downloads", params={"everyone": True}).status_code == 403
    every = env.alice.get("/api/downloads", params={"everyone": True}).json()
    assert {i["username"] for i in every["items"]} == {"alice", "bob"}
    alice_dl = env.alice.get("/api/downloads").json()["items"][0]["id"]
    assert env.bob.get(f"/api/downloads/{alice_dl}").status_code == 404
    assert env.alice.get("/api/downloads", params={"state": "bogus"}).status_code == 422
    assert (
        env.alice.get("/api/downloads", params={"state": "failed"}).json()["total"] == 0
    )


def test_next_peer_is_the_owners_or_an_admins_and_never_blames(env: Env) -> None:
    pick(env.bob, [("cd3_peer", CD3)])
    bob_dl = env.bob.get("/api/downloads").json()["items"][0]["id"]
    pick(env.alice, [("alice_peer", TAKK)])
    alice_dl = env.alice.get("/api/downloads").json()["items"][0]["id"]
    r = env.bob.post(f"/api/downloads/{alice_dl}/next-peer", headers=SAME)
    assert r.status_code == 404  # not bob's
    r = env.alice.post(f"/api/downloads/{bob_dl}/next-peer", headers=SAME)  # admin
    assert r.status_code == 200, r.text
    detail = r.json()
    # a pick has no other candidate: skipping it ends the request
    assert [a["outcome"] for a in detail["attempts"]] == ["skipped"]
    assert detail["item"]["track_state"] == "notDownloaded"
    with env.db.transaction() as conn:
        assert SlskdStore().get_peer(conn, "cd3_peer") is None
    r = env.alice.post(f"/api/downloads/{bob_dl}/next-peer", headers=SAME)
    assert r.status_code == 409  # nothing running


def test_pause_is_admin_only_refuses_picks_and_survives_a_restart(env: Env) -> None:
    rid = typed(env.alice)
    assert env.bob.post("/api/admin/queue/pause", headers=SAME).status_code == 403
    assert env.bob.get("/api/admin/queue").status_code == 403
    r = env.alice.post("/api/admin/queue/pause", headers=SAME)
    assert r.json() == {"paused": True}
    assert QueuePause(env.db).paused()  # stored: a restart stays paused
    files = [{"username": "alice_peer", "filename": TAKK}]
    r = env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": files},
                       headers=SAME)  # fmt: skip
    assert r.status_code == 409
    with env.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 0
    assert env.alice.post("/api/admin/queue/resume", headers=SAME).json() == {
        "paused": False
    }
    r = env.alice.post("/api/slskd/downloads", json={"request_id": rid, "files": files},
                       headers=SAME)  # fmt: skip
    assert r.status_code == 201


def access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                             '%s - "%s %s HTTP/%s" %d',
                             ("1.2.3.4:5", method, path, "1.1", status), None)  # fmt: skip


def test_successful_polls_stay_out_of_the_access_log() -> None:
    quiet = QuietPolls()
    assert not quiet.filter(access("GET", "/api/downloads?page=2", 200))
    assert not quiet.filter(access("GET", "/api/slskd/searches/4", 200))
    assert not quiet.filter(access("GET", "/api/requests/7", 200))
    assert quiet.filter(access("GET", "/api/downloads", 500))  # failures are logged
    assert quiet.filter(access("POST", "/api/requests", 200))  # changes are logged
    assert quiet.filter(access("GET", "/api/catalog/tracks?title=x", 200))


def test_state_follows_the_transfer_and_detail_asks_the_queue_place(env: Env) -> None:
    pick(env.alice, [("alice_peer", TAKK)])
    tid = next(iter(env.slskd.transfers))
    env.slskd.places[tid] = 7
    dl = env.alice.get("/api/downloads").json()["items"][0]["id"]
    list_place = env.alice.get("/api/downloads").json()["items"][0]["progress"]
    assert list_place["place_in_queue"] is None  # the polled list never asks the peer
    detail = env.alice.get(f"/api/downloads/{dl}").json()
    assert detail["item"]["progress"]["place_in_queue"] == 7  # "Queued, Remotely"
    assert env.alice.get("/api/downloads").json()["items"][0]["progress"][
        "place_in_queue"] == 7  # slskd stored it  # fmt: skip
    t = env.slskd.transfers[tid]
    env.slskd.transfers[tid] = replace(t, state="InProgress", bytes_transferred=1_000)
    env.downloader.poll()
    assert (
        env.alice.get(f"/api/downloads/{dl}").json()["item"]["state"] == "downloading"
    )


def test_the_queue_opens_with_the_users_own_defaults(env: Env) -> None:
    pick(env.alice, [("alice_peer", TAKK)])
    env.alice.put("/api/me/settings", json={"changes": {"downloads.state": "failed"}},
                  headers=SAME)  # fmt: skip
    assert env.alice.get("/api/downloads").json()["total"] == 0  # default: failed only
    assert (
        env.alice.get("/api/downloads", params={"state": ""}).json()["total"] == 1
    )  # all
    env.alice.put("/api/me/settings", json={"changes": {"downloads.page_size": 10}},
                  headers=SAME)  # fmt: skip
    assert (
        env.alice.get("/api/downloads", params={"state": ""}).json()["page_size"] == 10
    )
