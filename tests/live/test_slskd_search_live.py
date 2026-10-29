"""slskd search end to end on the test slskd (#101): catalog pick → request →
dispatch → slskd search. A few minutes (rate cap 4/min).

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_slskd_search_live.py -v -s
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from app.clients.musicbrainz import MusicBrainzClient
from app.clients.slskd import SlskdClient
from app.config import SlskdSettings
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.requests import RequestService
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_source import SlskdSource
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.slskd import SlskdStore
from app.stores.users import UsersStore

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
SLSKD_URL = os.environ.get("PV_TEST_SLSKD_URL", "http://127.0.0.1:8191")
SONGS = [("Feather", "Nujabes"), ("Roygbiv", "Boards of Canada")]


def test_search_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services")
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, 0).id
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz",), "download": ("slskd",)})
    catalog = CatalogService(db, prefs, [MusicBrainzClient()])
    requests = RequestService(db)
    slskd = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    source = SlskdSource(
        db, SlskdSearcher(db, slskd, SlskdGate(slskd, SlskdSettings()))
    )
    dispatch = DownloadDispatch(db, prefs, [source])

    ids = []
    for title, artist in SONGS:
        hit = catalog.search_tracks(uid, title, artist).items[0]
        song = catalog.resolve(uid, "musicbrainz", hit.id)
        print(f"song: {song.title!r} / {song.artist_name!r} → query names "
              f"{song.query_title!r} / {song.main_artist!r}")  # fmt: skip
        req = requests.create(uid, song.id, "catalog")
        assert dispatch.dispatch(uid, req.id) == "slskd"
        ids.append((req.id, song))
    source.stop()

    searcher = SlskdSearcher(db, slskd, SlskdGate(slskd, SlskdSettings()))
    for rid, song in ids:
        status = dispatch.status(uid, rid)
        with db.transaction() as conn:
            searches = SlskdStore().searches_for_request(conn, rid)
        cands = searcher.candidates(rid, song)
        print(f"request {rid}: {status.track_state}; searches "
              f"{[(s.strategy, s.query, s.id[:8]) for s in searches]}; "
              f"{len(cands)} candidates re-read, top 3: "
              f"{[(c.username, c.filename.rsplit(chr(92), 1)[-1], c.has_free_slot) for c in cands[:3]]}")  # fmt: skip
        assert status.track_state in ("found", "notFound")
    for line in caplog.messages:
        if "helpful peers" in line or "trusted" in line or "throttled" in line:
            print(" ", line)
    time.sleep(0)
