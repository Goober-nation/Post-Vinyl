"""Real downloads end to end on the test stack (#102): catalog pick → request →
dispatch → slskd search → download → monitor. ~10 songs, several minutes.
The test slskd reaches Soulseek through its own configured proxy.

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_slskd_download_live.py -v -s

Logs counts and outcomes (no pass/fail per song): a song that ends
notDownloaded is a result, not a failure. Checks every completed file exists
on disk with the size slskd reported.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from pathlib import Path

import pytest

from app.clients.musicbrainz import MusicBrainzClient
from app.clients.slskd import SlskdClient
from app.config import SlskdSettings
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.requests import RequestService
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_source import SlskdSource
from app.services.sources import SourcePreferences
from app.stores.database import Database
from app.stores.downloads import DownloadsStore
from app.stores.slskd import SlskdStore
from app.stores.users import UsersStore
from app.workers.download_monitor import DownloadMonitor

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
SLSKD_URL = os.environ.get("PV_TEST_SLSKD_URL", "http://127.0.0.1:8191")
# slskd's downloads dir inside the container is /music/downloads/complete/soulseek
SLSKD_DOWNLOADS_ON_HOST = Path(os.environ.get("MUSIC_HOST_DIR", "/nonexistent")) / (
    "downloads/complete/soulseek"
)
SONGS = [
    ("Feather", "Nujabes"),
    ("Roygbiv", "Boards of Canada"),
    ("Xtal", "Aphex Twin"),
    ("Archangel", "Burial"),
    ("Accordion", "Madvillain"),
    ("Teardrop", "Massive Attack"),
    ("Windowlicker", "Aphex Twin"),
    ("Dracula", "Tame Impala"),
    ("Nightcall", "Kavinsky"),
    ("Midnight City", "M83"),
    ("Strobe", "deadmau5"),
]
ENDED = {"downloaded", "notDownloaded", "badDownload", "notFound", "dropped"}
TIMEOUT_S = 30 * 60


def test_downloads_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.services")
    db = Database(tmp_path / "pv.db")
    db.migrate()
    with db.transaction() as conn:
        uid = UsersStore().upsert_on_login(conn, "alice", "e", False, 0).id
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz",), "download": ("slskd",)})
    catalog = CatalogService(db, prefs, [MusicBrainzClient()])
    requests = RequestService(db)
    slskd = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    settings = SlskdSettings()
    searcher = SlskdSearcher(db, slskd, SlskdGate(slskd, settings))
    downloader = SlskdDownloader(db, slskd, searcher, settings)
    source = SlskdSource(db, searcher, downloader)
    dispatch = DownloadDispatch(db, prefs, [source])
    monitor = DownloadMonitor(downloader.poll)
    monitor.start()

    rids: list[tuple[int, str]] = []
    for title, artist in SONGS:
        hit = catalog.search_tracks(uid, title, artist).items[0]
        song = catalog.resolve(uid, "musicbrainz", hit.id)
        req = requests.create(uid, song.id, "catalog")
        dispatch.dispatch(uid, req.id)
        rids.append((req.id, f"{artist} – {title}"))

    started = time.monotonic()
    while time.monotonic() - started < TIMEOUT_S:
        states = {rid: dispatch.status(uid, rid).track_state for rid, _ in rids}
        if all(s in ENDED for s in states.values()):
            break
        time.sleep(10)
    monitor.stop()
    source.stop()
    took = round(time.monotonic() - started)

    ends: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for rid, name in rids:
        st = dispatch.status(uid, rid)
        ends[st.track_state] += 1
        with db.transaction() as conn:
            rows = DownloadsStore().for_request(conn, rid)
            attempts = (
                SlskdStore().attempts_for_download(conn, rows[0].id) if rows else []
            )
        outcomes.update(a.outcome or "running" for a in attempts)
        line = f"{name}: {st.track_state}, attempts {[(a.rank, a.outcome) for a in attempts]}"
        if st.download_state == "completed" and st.file_location:
            path = SLSKD_DOWNLOADS_ON_HOST / st.file_location
            size = path.stat().st_size if path.exists() else None
            line += f", file {'exists' if size else 'MISSING'} ({size} bytes)"
            assert size == attempts[-1].size, (
                f"{name}: on-disk size differs from slskd's"
            )
        print(line)
    print(f"ended: {dict(ends)}; attempt outcomes: {dict(outcomes)}; {took} s")
    assert all(
        s in ENDED for s in (dispatch.status(uid, r).track_state for r, _ in rids)
    )
