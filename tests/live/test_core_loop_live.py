"""The whole core loop on the test stack (#105): MusicBrainz pick → request →
dispatch → slskd search → download → monitor → verify → tag → move →
Navidrome scan → linked, then slskd cleanup. Real services throughout, run by
``CoreLoopWorkers`` exactly as the app will. A few minutes.

    set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_core_loop_live.py -v -s

Logs where each song ended (no pass/fail per song). Files it adds to the test
library are removed at the end.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from pathlib import Path

import pytest

from app.clients.coverart import CoverArtClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.navidrome import NavidromeClient
from app.clients.slskd import SlskdClient
from app.config import FilesSettings, SlskdSettings, WorkersSettings
from app.services.catalog import CatalogService
from app.services.dispatch import DownloadDispatch
from app.services.encryption import SecretBox, generate_key
from app.services.files import FilesService
from app.services.pipeline import FilePipeline
from app.services.requests import RequestService
from app.services.slskd_cleanup import SlskdCleanup
from app.services.slskd_download import SlskdDownloader
from app.services.slskd_gate import SlskdGate
from app.services.slskd_search import SlskdSearcher
from app.services.slskd_source import SlskdSource
from app.services.sources import SourcePreferences
from app.services.tagging import TaggingService
from app.stores.database import Database
from app.stores.files import FilesStore
from app.stores.users import UsersStore
from app.workers.core_loop import CoreLoopWorkers

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
SLSKD_URL = os.environ.get("PV_TEST_SLSKD_URL", "http://127.0.0.1:8191")
NAVIDROME_URL = os.environ.get("PV_TEST_NAVIDROME_URL", "http://127.0.0.1:8190")
SONGS = [
    ("Glósóli", "Sigur Rós"),
    ("Idioteque", "Radiohead"),
]  # 2 confirm the loop (user)
TIMEOUT_S = 25 * 60


def test_core_loop_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app")
    music = Path(os.environ["MUSIC_HOST_DIR"])
    files_cfg, workers_cfg = (
        FilesSettings(str(music)),
        WorkersSettings(pipeline_every_seconds=5, cleanup_every_seconds=20),
    )
    db = Database(tmp_path / "pv.db")
    db.migrate()
    box = SecretBox(generate_key())
    user, password = os.environ["NAVIDROME_USERNAME"], os.environ["NAVIDROME_PASSWORD"]
    with db.transaction() as conn:
        uid = (
            UsersStore().upsert_on_login(conn, user, box.encrypt(password), True, 0).id
        )
    prefs = SourcePreferences(db, {"catalog": ("musicbrainz",), "download": ("slskd",)})
    mb, nd = MusicBrainzClient(), NavidromeClient(NAVIDROME_URL)
    slskd = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    sl_cfg = SlskdSettings()
    searcher = SlskdSearcher(db, slskd, SlskdGate(slskd, sl_cfg))
    downloader = SlskdDownloader(db, slskd, searcher, sl_cfg)
    dispatch = DownloadDispatch(db, prefs, [SlskdSource(db, searcher, downloader)])
    files = FilesService(db, nd, box, files_cfg)
    tagging = TaggingService(
        db, mb, ListenBrainzClient(), CoverArtClient(), box, files_cfg
    )
    pipeline = FilePipeline(db, files, tagging, workers_cfg)
    cleanup = SlskdCleanup(db, slskd, files_cfg)
    loop = CoreLoopWorkers(
        workers_cfg, dispatch, pipeline, downloader.poll, cleanup.tick
    )
    catalog, requests = CatalogService(db, prefs, [mb]), RequestService(db)

    rids = []
    for title, artist in SONGS:
        song = catalog.resolve(
            uid, "musicbrainz", catalog.search_tracks(uid, title, artist).items[0].id
        )
        rids.append(
            (
                requests.create(uid, song.id, "catalog").id,
                song.id,
                f"{artist} – {title}",
            )
        )
    loop.start()  # dispatches the waiting requests, then runs every worker
    placed: list[Path] = []
    started = time.monotonic()
    try:
        while time.monotonic() - started < TIMEOUT_S:
            with db.transaction() as conn:
                file_states = {
                    sid: (FilesStore().for_song(conn, sid) or None)
                    for _, sid, _ in rids
                }
            req_states = {
                rid: dispatch.status(uid, rid).track_state for rid, _, _ in rids
            }
            done = all(
                (f and f.file_state == "indexed")
                or req_states[rid] in ("notFound", "notDownloaded", "badDownload")
                for (rid, sid, _), f in zip(rids, file_states.values(), strict=True)
            )
            if done:
                break
            time.sleep(10)
        time.sleep(25)  # one more cleanup round
    finally:
        loop.stop()
    took = round(time.monotonic() - started)
    ends: Counter[str] = Counter()
    for rid, sid, name in rids:
        with db.transaction() as conn:
            f = FilesStore().for_song(conn, sid)
            tags = FilesStore().tagging_for_file(conn, f.id) if f else []
        st = dispatch.status(uid, rid).track_state
        end = f.file_state if f else st
        ends[end] += 1
        tag = tags[-1].status if tags else "-"
        print(
            f"{name}: request {st}, file {end}, tagging {tag}, at {f.file_location if f else '-'}"
        )
        if f and f.file_location.startswith("library/"):
            placed.append(music / f.file_location)
    print(
        f"ended: {dict(ends)} in {took} s; slskd transfers left: {len(slskd.list_downloads())}"
    )
    for line in caplog.messages:
        if "cleanup" in line.lower() or "forg" in line.lower():
            print(" ", line)
    for p in placed:  # tidy the test library
        if p.exists():
            p.unlink()
        for parent in (p.parent, p.parent.parent, p.parent.parent.parent):
            if (
                parent != music / "library"
                and parent.is_dir()
                and not any(parent.iterdir())
            ):
                parent.rmdir()
    nd.start_scan(nd.login(user, password))
    assert ends["indexed"] >= 1, "nothing made it all the way"
