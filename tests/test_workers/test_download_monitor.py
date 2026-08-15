"""
Tests for DownloadMonitor background worker.

Uses fake services, real EventHub, and real SQLite via temporary directories.
Event capture follows the same pattern as tests/test_sse.py: publish from a
background thread, then await asyncio.sleep(0) to process call_soon_threadsafe.
"""

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.playlist_store import PlaylistStore
from app.db.recs_store import RecsStore
from app.exceptions import SlskdConnectionError
from app.services.interfaces.download import QueueResult, Transfer
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.services.rec_playlist import RecPlaylistService
from app.sse import EventHub
from app.workers.download_monitor import DownloadMonitor


def _make_config(tmpdir, bad_peer_threshold=1):
    """Build a minimal test config."""
    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.download_dir = str(p / "download")
    paths.searches_dir = str(p / "searches")
    paths.discovery_dir = str(p / "discovery")
    # download_monitor.py resolves paths via the *_path properties in real
    # PathsConfig (music_dir / relative suffix) — mirror that here too.
    paths.download_path = p / "download"
    paths.searches_path = p / "searches"
    paths.discovery_path = p / "discovery"

    class MockDownload:
        check_interval = 15
        max_retries_per_track = 3
        peer_ban_days = 2

    MockDownload.bad_peer_threshold = bad_peer_threshold

    class MockBeets:
        enabled = False

    class MockRecs:
        comfort_zone_playlist_name = "Comfort Zone"
        fresh_picks_playlist_name = "Fresh Picks"
        deep_cuts_playlist_name = "Deep Cuts"
        rotation_trash_rating = 1

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.download = MockDownload()
    cfg.beets = MockBeets()
    cfg.recs = MockRecs()
    return cfg


class FakeDownloadService:
    """In-memory download service for testing."""

    def __init__(self, transfers=None):
        self._transfers = transfers or []
        self.queue_calls: list[dict] = []
        self._search_responses: dict[str, list[dict]] = {}
        self.fetch_search_responses_calls: list[str] = []
        self.deleted_transfers: list[str] = []
        self.delete_transfer_result = True

    def get_status(self) -> list[Transfer]:
        return list(self._transfers)

    def queue(self, username, files, search_id=None, destination=None):
        self.queue_calls.append(
            {
                "username": username,
                "files": files,
                "search_id": search_id,
                "destination": destination,
            }
        )
        return QueueResult(
            enqueued_count=len(files),
            failures=[],
            search_id=search_id,
        )

    def set_transfers(self, transfers):
        self._transfers = transfers

    def set_search_responses(self, search_id, responses):
        self._search_responses[search_id] = responses

    def fetch_search_responses(self, search_id):
        self.fetch_search_responses_calls.append(search_id)
        return self._search_responses.get(search_id, [])

    def delete_transfer(self, transfer_id):
        self.deleted_transfers.append(transfer_id)
        return self.delete_transfer_result


class FakeLibraryService:
    """In-memory library for tests.

    Playlist and search surfaces exist so the P6.7-7 add-on-completion hook
    (RecPlaylistService) can run end-to-end in tests that exercise a
    completed rec download.
    """

    def __init__(self):
        self.scan_count = 0
        self.playlists: list[PlaylistInfo] = []
        self.playlist_songs: dict[str, list[Song]] = {}
        self.songs_by_query: dict[str, list[Song]] = {}
        self.create_calls: list[str] = []

    def trigger_scan(self):
        self.scan_count += 1
        return True

    def list_playlists(self):
        return list(self.playlists)

    def create_playlist(self, name):
        playlist_id = f"pl-{len(self.playlists) + 1}"
        self.playlists.append(PlaylistInfo(playlist_id, name, 0))
        self.create_calls.append(name)
        return playlist_id

    def get_playlist_detail(self, playlist_id):
        name = next(
            (p.name for p in self.playlists if p.playlist_id == playlist_id), ""
        )
        return PlaylistDetail(
            playlist_id, name, list(self.playlist_songs.get(playlist_id, []))
        )

    def search_library(self, query):
        return list(self.songs_by_query.get(query, []))

    def add_to_playlist(self, playlist_id, song_ids):
        songs = self.playlist_songs.setdefault(playlist_id, [])
        for sid in song_ids:
            songs.append(Song(sid, "", "", "", "", 0, 0, None, None, None, None, 0, False))
        return True


class FakeBeetsService:
    """Stands in for BeetsService — moves the file like the old _move_file()
    did (per-username subfolder, "(1)" collision suffix) rather than
    shelling out to a real beets install, matched=True unless overridden."""

    def __init__(self, config, matched=True):
        self._config = config
        self.matched = matched
        self.calls: list[tuple] = []

    def import_file(
        self,
        source: Path,
        is_rec: bool,
        title=None,
        artist=None,
        category=None,
        library=None,
        mbid=None,
    ):
        from app.services.beets import BeetsImportResult

        self.calls.append((source, is_rec, title, artist, category, library, mbid))
        target_root = Path(
            self._config.paths.discovery_dir
            if is_rec
            else self._config.paths.searches_dir
        )
        target_dir = target_root / source.parent.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if target.exists():
            stem, suffix, counter = target.stem, target.suffix, 1
            while target.exists():
                target = target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
        source.rename(target)
        return BeetsImportResult(matched=self.matched, target_path=target)


def _transfer(
    tid, username, filename, size, state, progress=0.0, speed=None, is_rec=False
):
    return Transfer(
        transfer_id=tid,
        username=username,
        filename=filename,
        size=size,
        state=state,
        progress=progress,
        speed=speed,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc)
        if state in ("completed", "failed", "cancelled")
        else None,
        is_rec_download=is_rec,
    )


@pytest.fixture
def tmp_config(tmp_path):
    return _make_config(str(tmp_path))


@pytest.fixture
def db(tmp_config):
    database = Database(tmp_config)
    database.initialize_schema()
    yield database
    database.close()


def _run_poll_and_capture(monitor, hub):
    """Run poll_once() in a background thread and capture SSE events.

    Pattern from tests/test_sse.py: publish from thread, await sleep(0),
    then drain subscriber queue.  Returns (poll_result, events_list).
    """
    result_holder: dict = {}

    def _poll():
        result_holder["result"] = monitor.poll_once()

    async def _capture():
        sub = hub.subscribe()
        t = threading.Thread(target=_poll)
        t.start()
        t.join()
        await asyncio.sleep(0)
        captured: list[tuple[str, dict]] = []
        while not sub.queue.empty():
            ev = sub.queue.get_nowait()
            captured.append((ev.event_type, ev.data))
        hub.unsubscribe(sub)
        return captured

    captured_events = asyncio.run(_capture())
    return result_holder["result"], captured_events


class TestPollOnceBasic:
    def test_empty_transfers(self, tmp_config, db):
        hub = EventHub()
        download_svc = FakeDownloadService([])
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["transfers_seen"] == 0
        assert result["moved"] == []
        assert result["scan_triggered"] is False
        assert result["retried"] == []

    def test_states_no_files_on_disk(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-complete",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
            _transfer(
                "t-errored",
                "absolutelyjaked2",
                "music\\Clipse\\Clipse - Let God Sort Em Out\\03 - P.O.V.mp3",
                10455731,
                "failed",
                progress=0.0,
            ),
            _transfer(
                "t-cancelled",
                "cr4sh0verr1de",
                "media\\Sorted\\Singles\\Queen-Awesome 80's (disc 1)-01-Another One Bites the Dust.mp3",
                5190425,
                "cancelled",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        assert _result["transfers_seen"] == 3
        assert _result["scan_triggered"] is False

        event_types = {e[0] for e in events}
        assert "transfer.completed" in event_types
        assert "transfer.failed" in event_types
        assert "transfer.cancelled" not in event_types

        completed_ev = next(e for e in events if e[0] == "transfer.completed")
        assert completed_ev[1]["target_dir"] == ""

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False

    def test_started_event(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer("t-new", "peer1", "song.mp3", 1000, "queued"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        started = [e for e in events if e[0] == "transfer.started"]
        assert len(started) == 1
        assert started[0][1]["transfer_id"] == "t-new"

    def test_started_event_on_pending_adoption(self, tmp_config, db):
        """A queue-time pending row adopted by its slskd transfer emits started."""
        store = DownloadStore(db)
        store.insert_pending("search-1", "peer1", "song.mp3", 1000, False)

        hub = EventHub()
        transfers = [
            _transfer(
                "uuid-123", "peer1", "song.mp3", 1000, "downloading", progress=5.0
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        started = [e for e in events if e[0] == "transfer.started"]
        assert len(started) == 1
        assert started[0][1]["transfer_id"] == "uuid-123"
        row = store.get_transfer("uuid-123")
        assert row is not None
        assert row["search_id"] == "search-1"

    def test_progress_event(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-dl",
                "peer1",
                "song.mp3",
                1000,
                "downloading",
                progress=45.0,
                speed=1024,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        progress_ev = [e for e in events if e[0] == "transfer.progress"]
        assert len(progress_ev) == 1
        assert progress_ev[0][1]["progress"] == 45
        assert progress_ev[0][1]["speed"] == 1024


class TestFileMove:
    def test_file_move_cycle(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-move",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        source_dir = (
            Path(tmp_config.paths.download_dir)
            / "complete"
            / "soulseek"
            / "ShaLaLaLaLee"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "01 - Bohemian Rhapsody.mp3").write_text("fake mp3 data")

        target_dir = Path(tmp_config.paths.searches_dir) / "ShaLaLaLaLee"
        target_dir.mkdir(parents=True, exist_ok=True)

        tmp_config.beets.enabled = True
        monitor = DownloadMonitor(
            tmp_config,
            download_svc,
            lib_svc,
            db,
            hub,
            interval=15,
            beets_service=FakeBeetsService(tmp_config),
        )

        result, events = _run_poll_and_capture(monitor, hub)

        assert result["scan_triggered"] is True
        assert lib_svc.scan_count == 1
        assert len(result["moved"]) == 1

        target = target_dir / "01 - Bohemian Rhapsody.mp3"
        assert target.exists()

        store = DownloadStore(db)
        assert store.file_moved("t-move")

        completed_ev = next(e for e in events if e[0] == "transfer.completed")
        assert completed_ev[1]["target_dir"] == str(target_dir)

    def test_file_move_collision(self, tmp_config, db):
        hub = EventHub()
        transfers = [
            _transfer(
                "t-collide",
                "ShaLaLaLaLee",
                "@@hehse\\1\\Temp\\Masha\\Queen\\1981 - Greatest Hits\\01 - Bohemian Rhapsody.mp3",
                13065767,
                "completed",
                progress=100.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        source_dir = (
            Path(tmp_config.paths.download_dir)
            / "complete"
            / "soulseek"
            / "ShaLaLaLaLee"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "01 - Bohemian Rhapsody.mp3").write_text("fake mp3 data")

        target_dir = Path(tmp_config.paths.searches_dir) / "ShaLaLaLaLee"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "01 - Bohemian Rhapsody.mp3").write_text("pre-existing")

        tmp_config.beets.enabled = True
        monitor = DownloadMonitor(
            tmp_config,
            download_svc,
            lib_svc,
            db,
            hub,
            interval=15,
            beets_service=FakeBeetsService(tmp_config),
        )

        result, _events = _run_poll_and_capture(monitor, hub)

        moved = result["moved"]
        assert len(moved) == 1
        assert "(1)" in Path(moved[0]).name

    def test_rec_download_moves_to_discovery(self, tmp_config, db):
        """A rec download (DB row is_rec_download=1) moves to discovery/, not searches/."""
        # Seed a queue-time pending row with the rec flag set
        store = DownloadStore(db)
        store.insert_pending(
            "rec-search-1",
            "astuary",
            "some\\dir\\Tangerine Sour.mp3",
            5000,
            True,
        )
        # Seed the queued recommendation row the hook must update
        db.execute(
            "INSERT INTO recommendations (source, artist, track, mbid, status, "
            "search_id, created_at) VALUES ('deep_cuts', 'Emancipator', "
            "'Tangerine Sour', NULL, 'queued', 'rec-search-1', ?)",
            (int(time.time()),),
        )

        hub = EventHub()
        transfers = [
            _transfer(
                "t-rec",
                "astuary",
                "some\\dir\\Tangerine Sour.mp3",
                5000,
                "completed",
                progress=100.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        # P6.7-7 (S12): the completed rec's track is findable in the library,
        # so the add-on-completion hook can add it to its category playlist.
        lib_svc.songs_by_query["Tangerine Sour"] = [
            Song(
                "song-tangerine", "Tangerine Sour", "Emancipator", "Dusk to Dawn",
                "", 300, 5000, None, None, None, None, 0, False,
            )
        ]

        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "astuary"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "Tangerine Sour.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        monitor = DownloadMonitor(
            tmp_config,
            download_svc,
            lib_svc,
            db,
            hub,
            interval=15,
            beets_service=FakeBeetsService(tmp_config),
        )

        result, events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1
        assert str(tmp_config.paths.discovery_dir) in result["moved"][0]
        assert str(tmp_config.paths.searches_dir) not in result["moved"][0]
        # Rec completion hook fired: recommendation row linked via search_id
        rec = db.fetch_one(
            "SELECT status, download_id, playlist_id FROM recommendations "
            "WHERE search_id = ?",
            ("rec-search-1",),
        )
        assert rec is not None
        assert rec["status"] == "downloaded"
        assert rec["download_id"] == "t-rec"
        # P6.7-7 (S12): the rec also reached its category playlist, and the
        # playlist was created lazily for it.
        assert rec["playlist_id"] is not None
        assert "Deep Cuts" in lib_svc.create_calls
        assert "song-tangerine" in {
            s.song_id for s in lib_svc.playlist_songs[rec["playlist_id"]]
        }
        completed_ev = next(e for e in events if e[0] == "transfer.completed")
        assert str(tmp_config.paths.discovery_dir) in completed_ev[1]["target_dir"]

    def test_rec_playlist_retries_after_index_lag(self, tmp_config, db):
        """A later monitor poll links a rec once Navidrome indexes the import."""
        store = DownloadStore(db)
        store.insert_pending(
            "rec-search-lag",
            "astuary",
            "some\\dir\\Tangerine Sour.mp3",
            5000,
            True,
        )
        db.execute(
            "INSERT INTO recommendations (source, artist, track, mbid, status, "
            "search_id, created_at) VALUES ('fresh_picks', 'Emancipator', "
            "'Tangerine Sour', NULL, 'queued', 'rec-search-lag', ?)",
            (int(time.time()),),
        )

        transfer = _transfer(
            "t-rec-lag",
            "astuary",
            "some\\dir\\Tangerine Sour.mp3",
            5000,
            "completed",
            progress=100.0,
        )
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "astuary"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "Tangerine Sour.mp3").write_text("fake mp3 data")

        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService([transfer]),
            lib_svc,
            db,
            EventHub(),
            interval=15,
            beets_service=FakeBeetsService(tmp_config),
        )
        tmp_config.beets.enabled = True

        _run_poll_and_capture(monitor, monitor._event_hub)
        first = db.fetch_one(
            "SELECT status, playlist_id FROM recommendations WHERE search_id = ?",
            ("rec-search-lag",),
        )
        assert first == {"status": "downloaded", "playlist_id": None}

        lib_svc.songs_by_query["Tangerine Sour"] = [
            Song(
                "song-tangerine", "Tangerine Sour", "Emancipator", "Dusk to Dawn",
                "", 300, 5000, None, None, None, None, 0, False,
            )
        ]
        _run_poll_and_capture(monitor, monitor._event_hub)

        second = db.fetch_one(
            "SELECT status, playlist_id FROM recommendations WHERE search_id = ?",
            ("rec-search-lag",),
        )
        assert second["status"] == "downloaded"
        assert second["playlist_id"] is not None

    def test_completed_rec_is_reconciled_when_first_poll_was_missed(
        self, tmp_config, db
    ):
        """A completed transfer cannot leave its rec row stuck in queued."""
        store = DownloadStore(db)
        store.insert_pending(
            "rec-search-missed",
            "astuary",
            "some\\dir\\Tangerine Sour.mp3",
            5000,
            True,
        )
        db.execute(
            "INSERT INTO recommendations (source, artist, track, mbid, status, "
            "search_id, created_at) VALUES ('fresh_picks', 'Emancipator', "
            "'Tangerine Sour', NULL, 'queued', 'rec-search-missed', ?)",
            (int(time.time()),),
        )
        transfer = _transfer(
            "t-rec-missed",
            "astuary",
            "some\\dir\\Tangerine Sour.mp3",
            5000,
            "completed",
            progress=100.0,
        )
        store.upsert_transfer(transfer)
        store.mark_file_moved("t-rec-missed", "/music/Discovery/Fresh_Picks")

        lib_svc = FakeLibraryService()
        lib_svc.songs_by_query["Tangerine Sour"] = [
            Song(
                "song-tangerine", "Tangerine Sour", "Emancipator", "Dusk to Dawn",
                "", 300, 5000, None, None, None, None, 0, False,
            )
        ]
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService([transfer]),
            lib_svc,
            db,
            EventHub(),
            interval=15,
            beets_service=FakeBeetsService(tmp_config),
        )

        _run_poll_and_capture(monitor, monitor._event_hub)

        rec = db.fetch_one(
            "SELECT status, download_id, playlist_id FROM recommendations "
            "WHERE search_id = ?",
            ("rec-search-missed",),
        )
        assert rec == {
            "status": "downloaded",
            "download_id": "t-rec-missed",
            "playlist_id": "pl-1",
        }

    def test_rec_playlist_matches_beets_normalized_metadata(self, tmp_config, db):
        """Fresh Picks can be indexed under a shortened MusicBrainz title."""
        recs_store = RecsStore(db)
        rec_id = recs_store.insert_rec(
            "fresh_picks",
            "lovehead",
            "sommerwind ep",
            None,
            "downloaded",
        )
        library = FakeLibraryService()
        library.songs_by_query["lovehead"] = [
            Song(
                "song-sommerwind", "sommerwind", "lovehead", "sommerwind",
                "", 180, 5000, None, None, None, None, 0, False,
            )
        ]

        service = RecPlaylistService(tmp_config, library, recs_store, PlaylistStore(db))
        assert service.add_downloaded_to_playlist(
            {
                "id": rec_id,
                "source": "fresh_picks",
                "artist": "lovehead",
                "track": "sommerwind ep",
            }
        )
        assert recs_store.get_rec(rec_id)["playlist_id"] == "pl-1"


class TestBeetsImport:
    """P6.6-2/-4: beets owns the import path that _move_file() used to."""

    def _completed(self, filename="a\\b\\01 - Track.mp3"):
        return [
            _transfer("t-b", "peerone", filename, 5000, "completed", progress=100.0)
        ]

    def _seed_source(self, tmp_config, *relative_parts):
        source = Path(
            tmp_config.paths.download_dir,
            "complete",
            "soulseek",
            "peerone",
            *relative_parts,
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fake mp3 data")
        return source

    def test_disabled_beets_leaves_the_file_in_place(self, tmp_config, db):
        """No fallback to the retired _move_file(): with beets off, a
        completed download stays where slskd put it."""
        hub = EventHub()
        source = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        beets = FakeBeetsService(tmp_config)

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert result["scan_triggered"] is False
        assert beets.calls == []
        assert source.exists()
        assert not DownloadStore(db).file_moved("t-b")

    def test_exact_reported_path_wins_over_a_basename_twin(self, tmp_config, db):
        """The _move_file() defect this replaces: a first-basename-match glob
        could move a *different* download that happened to share a filename."""
        hub = EventHub()
        wanted = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        decoy = self._seed_source(tmp_config, "other", "01 - Track.mp3")
        decoy.write_text("WRONG FILE")
        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )
        _run_poll_and_capture(monitor, hub)

        assert beets.calls[0][0] == wanted
        assert decoy.exists(), "the same-named file from another download was moved"

    def test_falls_back_to_basename_when_exact_path_is_absent(self, tmp_config, db):
        """slskd doesn't always mirror the peer's directory structure
        locally, so the old basename search stays as a fallback."""
        hub = EventHub()
        source = self._seed_source(tmp_config, "01 - Track.mp3")
        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert beets.calls[0][0] == source
        assert len(result["moved"]) == 1

    def test_failed_import_does_not_mark_the_file_moved(self, tmp_config, db):
        hub = EventHub()
        source = self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        tmp_config.beets.enabled = True

        class FailingBeets:
            def import_file(
                self,
                source,
                is_rec,
                title=None,
                artist=None,
                category=None,
                library=None,
                mbid=None,
            ):
                from app.services.beets import BeetsImportResult

                return BeetsImportResult(False, None, "beet blew up")

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=FailingBeets(),
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert result["moved"] == []
        assert source.exists()
        assert not DownloadStore(db).file_moved("t-b")

    def test_unmatched_import_is_flagged_but_still_lands(self, tmp_config, db):
        """P6.6-4: an untaggable file imports and is flagged — not dropped,
        not quarantined."""
        hub = EventHub()
        self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        tmp_config.beets.enabled = True

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=FakeBeetsService(tmp_config, matched=False),
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert len(result["moved"]) == 1
        assert Path(result["moved"][0]).exists()
        row = db.fetch_one("SELECT import_unmatched FROM downloads WHERE id = 't-b'")
        assert row["import_unmatched"] == 1

    def test_matched_import_is_not_flagged(self, tmp_config, db):
        hub = EventHub()
        self._seed_source(tmp_config, "a", "b", "01 - Track.mp3")
        tmp_config.beets.enabled = True

        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=FakeBeetsService(tmp_config, matched=True),
        )
        _run_poll_and_capture(monitor, hub)

        row = db.fetch_one("SELECT import_unmatched FROM downloads WHERE id = 't-b'")
        assert row["import_unmatched"] == 0


class TestRetry:
    def test_retry_path_with_search_id(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer(
                "t-retry",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
                progress=0.0,
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-1", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
            True,
            "recording-1",
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "absolutelyjaked2"

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

        retry_row = db.fetch_one(
            "SELECT search_id, is_library_download, mb_recording_id, retry_count "
            "FROM downloads WHERE username = ? AND state = 'queued'",
            (retry_peer,),
        )
        assert retry_row is not None
        assert retry_row["search_id"] == "search-retry-1"
        assert retry_row["is_library_download"] == 1
        assert retry_row["mb_recording_id"] == "recording-1"
        assert retry_row["retry_count"] == 1

    def test_superseded_transfer_is_removed_from_slskd(self, tmp_path, db):
        """A 'failed' state is often transient — slskd (or the peer) can
        still finish the original after musica re-queued the track
        elsewhere, and both copies land. Live-confirmed 2026-08-11: a
        5-track Comfort Zone pull downloaded 2 tracks twice."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        download_svc = FakeDownloadService(
            [
                _transfer(
                    "t-dupe",
                    "absolutelyjaked2",
                    "music\\Clipse\\03 - P.O.V.mp3",
                    10455731,
                    "failed",
                )
            ]
        )
        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        download_svc.set_search_responses(
            "search-dupe-1", json.loads(fixture_path.read_text())
        )
        DownloadStore(db).insert_pending(
            "search-dupe-1",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(
            cfg, download_svc, FakeLibraryService(), db, hub, interval=15
        )
        _run_poll_and_capture(monitor, hub)

        assert download_svc.queue_calls, "expected a retry to be queued"
        assert download_svc.deleted_transfers == ["t-dupe"]

    def test_failed_slskd_removal_does_not_break_the_retry(self, tmp_path, db):
        """Removal is best-effort — losing it only risks the duplicate we
        already had, so it must never turn a good retry into a failure."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        download_svc = FakeDownloadService(
            [
                _transfer(
                    "t-dupe2",
                    "absolutelyjaked2",
                    "music\\Clipse\\03 - P.O.V.mp3",
                    10455731,
                    "failed",
                )
            ]
        )
        download_svc.delete_transfer = lambda _tid: (_ for _ in ()).throw(
            RuntimeError("slskd unreachable")
        )
        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        download_svc.set_search_responses(
            "search-dupe-2", json.loads(fixture_path.read_text())
        )
        DownloadStore(db).insert_pending(
            "search-dupe-2",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(
            cfg, download_svc, FakeLibraryService(), db, hub, interval=15
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

    def test_no_retry_when_no_search_id(self, tmp_path, db):
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer("t-noretry", "peerX", "track.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False
        assert "No search context" in failed_ev[1]["error"]

    def test_retry_refetches_from_slskd_after_restart(self, tmp_path, db):
        """Retry must survive a restart with musica holding no copy of the
        results. After a restart the in-memory cache is empty, so the search
        is re-read from slskd by search_id — which is a *re-read of the same
        completed search*, not a new one, so the candidate pool is unchanged.
        Verified live 2026-08-11: slskd still served all 250 responses for
        musica's oldest search, and they survived a slskd restart."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=3)
        transfers = [
            _transfer(
                "t-persisted",
                "failingpeer",
                "music\\Band\\01 - Track.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())

        store = DownloadStore(db)
        store.insert_pending(
            "search-persisted-1",
            "failingpeer",
            "music\\Band\\01 - Track.mp3",
            10455731,
            False,
        )
        # slskd still holds this search — nothing in musica's DB does.
        download_svc.set_search_responses("search-persisted-1", responses)

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "failingpeer"
        # Sourced from slskd, keyed by the search_id on the download row.
        assert download_svc.fetch_search_responses_calls == ["search-persisted-1"]

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True


class TestPeerBlocking:
    def test_peer_blocked_after_threshold(self, tmp_config, db):
        hub = EventHub()

        transfers1 = [
            _transfer("t-fail1", "badpeer", "song1.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers1)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )
        _run_poll_and_capture(monitor, hub)

        transfers2 = [
            _transfer("t-fail2", "badpeer", "song2.mp3", 200, "failed"),
        ]
        download_svc.set_transfers(transfers2)
        _result, events = _run_poll_and_capture(monitor, hub)

        store = DownloadStore(db)
        assert store.is_peer_blocked("badpeer")

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert "blocked" in failed_ev[1]["error"].lower()
        assert failed_ev[1]["will_retry"] is False

    def test_expired_block_is_lifted_and_peer_retried(self, tmp_path, db):
        """A block older than config.download.peer_ban_days must be lifted
        so the peer is eligible again, not treated as permanently banned."""
        import time

        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=1)
        cfg.download.peer_ban_days = 2
        transfers = [
            _transfer(
                "t-retry-expired",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-expired", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-expired",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )
        # Pre-block the alternative peer the fixture would otherwise pick
        # first ("fivepointsquare" — first candidate with a free slot and
        # an actual matching file), but backdate it past the 2-day window
        # so it must be treated as eligible again rather than skipped.
        store.increment_peer_failure("fivepointsquare")
        store.set_peer_blocked("fivepointsquare")
        store._db.execute(
            "UPDATE peers SET blocked_at = ? WHERE username = ?",
            (int(time.time()) - 3 * 86400, "fivepointsquare"),
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)
        _run_poll_and_capture(monitor, hub)

        assert not store.is_peer_blocked("fivepointsquare", 2 * 86400)
        assert store.get_peer_failure_count("fivepointsquare") == 0
        assert download_svc.queue_calls[0]["username"] == "fivepointsquare"

    def test_blocked_peer_still_retries_with_alternative(self, tmp_path, db):
        """Blocking the failing peer must not short-circuit retry when an
        alternative peer is available in the stored search responses."""
        hub = EventHub()
        cfg = _make_config(str(tmp_path), bad_peer_threshold=1)
        transfers = [
            _transfer(
                "t-retry-blocked",
                "absolutelyjaked2",
                "music\\Clipse\\03 - P.O.V.mp3",
                10455731,
                "failed",
            ),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()

        fixture_path = (
            Path(__file__).parent.parent / "fixtures" / "slskd_search_responses.json"
        )
        responses = json.loads(fixture_path.read_text())
        download_svc.set_search_responses("search-retry-2", responses)

        store = DownloadStore(db)
        store.insert_pending(
            "search-retry-2",
            "absolutelyjaked2",
            "music\\Clipse\\03 - P.O.V.mp3",
            10455731,
            False,
        )

        monitor = DownloadMonitor(cfg, download_svc, lib_svc, db, hub, interval=15)

        _result, events = _run_poll_and_capture(monitor, hub)

        assert store.is_peer_blocked("absolutelyjaked2")
        assert len(download_svc.queue_calls) >= 1
        retry_peer = download_svc.queue_calls[0]["username"]
        assert retry_peer != "absolutelyjaked2"

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is True

    def test_blocked_peer_no_alternative_gives_up(self, tmp_config, db):
        """Blocking with no alternative peer available still gives up, but
        the retry budget is what stops it, not the block itself."""
        hub = EventHub()
        transfers = [
            _transfer("t-fail-noalt", "lonelypeer", "song.mp3", 100, "failed"),
        ]
        download_svc = FakeDownloadService(transfers)
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=15
        )

        _result, events = _run_poll_and_capture(monitor, hub)

        store = DownloadStore(db)
        assert store.is_peer_blocked("lonelypeer")

        failed_ev = next(e for e in events if e[0] == "transfer.failed")
        assert failed_ev[1]["will_retry"] is False
        assert "blocked" in failed_ev[1]["error"].lower()
        assert "no search context" in failed_ev[1]["error"].lower()


class TestStartStop:
    def test_start_and_stop(self, tmp_config, db):
        hub = EventHub()
        download_svc = FakeDownloadService([])
        lib_svc = FakeLibraryService()
        monitor = DownloadMonitor(
            tmp_config, download_svc, lib_svc, db, hub, interval=0.1
        )

        monitor.start()
        assert monitor._thread is not None
        assert monitor._thread.is_alive()

        monitor.stop()
        assert monitor._thread is None


class TestHousekeeping:
    """P6.5 review fixes (2026-08-11): stale-pending reaping and the search
    retention sweep, both driven off the existing poll loop."""

    def test_stale_pending_row_is_failed_and_announced(self, tmp_config, db):
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "stuck.mp3", 100, False)
        db.execute("UPDATE downloads SET created_at = created_at - 600")
        assert store.has_active_manual_downloads() is True

        monitor = DownloadMonitor(
            tmp_config, FakeDownloadService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        # The gate RecPuller waits on is released...
        assert store.has_active_manual_downloads() is False
        # ...and the user is told why, rather than the row just vanishing.
        failed = [d for name, d in events if name == "transfer.failed"]
        assert len(failed) == 1
        assert failed[0]["will_retry"] is False
        assert "never picked up" in failed[0]["error"]

    def test_fresh_pending_row_is_left_alone(self, tmp_config, db):
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "recent.mp3", 100, False)

        monitor = DownloadMonitor(
            tmp_config, FakeDownloadService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        _result, events = _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is True
        assert [name for name, _ in events if name == "transfer.failed"] == []

    def test_reaping_runs_even_when_slskd_is_unreachable(self, tmp_config, db):
        """slskd being down is exactly when rows go unadopted, and poll_once
        returns early on a connection error — so housekeeping runs first."""
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "stuck.mp3", 100, False)
        db.execute("UPDATE downloads SET created_at = created_at - 600")

        class UnreachableService(FakeDownloadService):
            def get_status(self):
                raise SlskdConnectionError("http://slskd:5030", "refused")

        monitor = DownloadMonitor(
            tmp_config, UnreachableService([]), FakeLibraryService(), db, hub,
            interval=15,
        )
        result, _events = _run_poll_and_capture(monitor, hub)

        assert "error" in result
        assert store.has_active_manual_downloads() is False

class TestOrphanReconciliation:
    """A transfer slskd stops reporting used to sit in 'downloading' forever:
    phantom UI row, no retry (retry fires on a reported 'failed' that never
    comes), and a permanent block on rec queueing if it was manual."""

    def _adopt(self, db, monitor, hub, *, is_rec=False, search_id="s-orphan"):
        """Get a row to the adopted/downloading state the normal way."""
        store = DownloadStore(db)
        store.insert_pending(search_id, "peer1", "song.mp3", 100, is_rec)
        monitor._download_service._transfers = [
            _transfer("uuid-1", "peer1", "song.mp3", 100, "downloading")
        ]
        _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "downloading"
        return store

    def _monitor(self, cfg, db, hub, responses=None):
        svc = FakeDownloadService([])
        if responses is not None:
            svc._search_responses["s-orphan"] = responses
        return DownloadMonitor(cfg, svc, FakeLibraryService(), db, hub, interval=15)

    def test_still_reported_transfer_is_untouched(self, tmp_config, db):
        """Restarting musica alone leaves slskd transferring — those rows
        resync normally and must never be cancelled."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)

        for _ in range(5):
            _run_poll_and_capture(monitor, hub)

        assert store.get_transfer("uuid-1")["state"] == "downloading"

    def test_orphan_is_failed_after_the_grace_period(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)

        # slskd stops reporting it.
        monitor._download_service._transfers = []

        # One miss is within grace (default 2) — still live.
        _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "downloading"

        _result, events = _run_poll_and_capture(monitor, hub)
        assert store.get_transfer("uuid-1")["state"] == "failed"
        failed = [d for name, d in events if name == "transfer.failed"]
        assert len(failed) == 1
        assert "stopped reporting" in failed[0]["error"]

    def test_a_blip_does_not_kill_a_healthy_transfer(self, tmp_config, db):
        """One truncated status response must not orphan anything — the
        miss counter resets as soon as the row is reported again."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        transfers = monitor._download_service._transfers

        for _ in range(4):
            monitor._download_service._transfers = []
            _run_poll_and_capture(monitor, hub)
            monitor._download_service._transfers = transfers
            _run_poll_and_capture(monitor, hub)

        assert store.get_transfer("uuid-1")["state"] == "downloading"

    def test_orphan_releases_the_rec_queue_gate(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub, is_rec=False)
        assert store.has_active_manual_downloads() is True

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is False

    def test_orphan_is_retried_from_persisted_responses(self, tmp_config, db):
        """The payoff of P6.5-4: re-pick an alternative peer without
        re-issuing a search."""
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        monitor._download_service.set_search_responses(
            "s-orphan",
            [
                {
                    "username": "peer1",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                },
                {
                    "username": "peer2",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                },
            ],
        )

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        result, events = _run_poll_and_capture(monitor, hub)

        # Re-queued from the *other* peer, sourced by re-reading the same
        # completed search from slskd — never by starting a new one.
        assert result["retried"] == ["uuid-1"]
        assert monitor._download_service.queue_calls[-1]["username"] == "peer2"
        assert monitor._download_service.fetch_search_responses_calls == ["s-orphan"]
        failed = [d for name, d in events if name == "transfer.failed"]
        assert failed[0]["will_retry"] is True

    def test_orphan_respects_the_retry_budget(self, tmp_config, db):
        hub = EventHub()
        monitor = self._monitor(tmp_config, db, hub)
        store = self._adopt(db, monitor, hub)
        monitor._download_service.set_search_responses(
            "s-orphan",
            [
                {
                    "username": "peer2",
                    "files": [{"filename": "song.mp3", "size": 100}],
                    "hasFreeUploadSlot": True,
                }
            ],
        )
        db.execute("UPDATE downloads SET retry_count = 99 WHERE id = 'uuid-1'")

        monitor._download_service._transfers = []
        _run_poll_and_capture(monitor, hub)
        _result, events = _run_poll_and_capture(monitor, hub)

        failed = [d for name, d in events if name == "transfer.failed"]
        assert failed[0]["will_retry"] is False
        assert "Max retries exceeded" in failed[0]["error"]

    def test_unadopted_pending_rows_are_left_to_the_reaper(self, tmp_config, db):
        """A 'pending:' row has never been confirmed by slskd, so its absence
        from a status poll means nothing — fail_stale_pending owns those."""
        hub = EventHub()
        store = DownloadStore(db)
        store.insert_pending("s1", "peer1", "fresh.mp3", 100, False)

        monitor = self._monitor(tmp_config, db, hub)
        for _ in range(5):
            _run_poll_and_capture(monitor, hub)

        assert store.has_active_manual_downloads() is True


class TestImportIntentResolution:
    """P-MB-1 wiring: the monitor must recover "what the user actually
    asked for" and hand it to beets, so the import can be constrained to
    it instead of trusting the downloaded file's own tags."""

    def test_manual_search_intent_is_passed_to_beets(self, tmp_config, db):
        from app.db.search_store import SearchStore

        SearchStore(db).insert_search("s-manual", "Jóga", "Björk", "completed")
        store = DownloadStore(db)
        store.insert_pending("s-manual", "peerone", "a\\Joga.mp3", 5000, False)

        hub = EventHub()
        transfers = [
            _transfer("t-manual", "peerone", "a\\Joga.mp3", 5000, "completed",
                      progress=100.0)
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "peerone"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Joga.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        _source, is_rec, title, artist, category, _library, _mbid = beets.calls[0]
        assert is_rec is False
        assert title == "Jóga"
        assert artist == "Björk"
        assert category is None

    def test_rec_intent_is_passed_to_beets(self, tmp_config, db):
        store = DownloadStore(db)
        store.insert_pending(
            "rec-search-2", "astuary", "some\\dir\\Tangerine Sour.mp3", 5000, True
        )
        db.execute(
            "INSERT INTO recommendations (source, artist, track, mbid, status, "
            "search_id, created_at) VALUES ('deep_cuts', 'Emancipator', "
            "'Tangerine Sour', NULL, 'queued', 'rec-search-2', ?)",
            (int(time.time()),),
        )

        hub = EventHub()
        transfers = [
            _transfer(
                "t-rec2", "astuary", "some\\dir\\Tangerine Sour.mp3", 5000,
                "completed", progress=100.0,
            )
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "astuary"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "Tangerine Sour.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        _source, is_rec, title, artist, category, _library, _mbid = beets.calls[0]
        assert is_rec is True
        assert title == "Tangerine Sour"
        assert artist == "Emancipator"
        assert category == "deep_cuts"

    def test_rec_with_unknown_source_passes_it_through(self, tmp_config, db):
        """A rec row with a source value no category profile maps (legacy/
        unknown) has no beets destination — the monitor passes it through
        unchanged and the real BeetsService fails the import rather than
        guessing (P6.7-0b)."""
        store = DownloadStore(db)
        store.insert_pending(
            "rec-search-nosrc", "astuary", "legacy.mp3", 5000, True
        )
        db.execute(
            "INSERT INTO recommendations (source, artist, track, mbid, status, "
            "search_id, created_at) VALUES ('weekly_jams', 'Some Artist', "
            "'Legacy Track', NULL, 'queued', 'rec-search-nosrc', ?)",
            (int(time.time()),),
        )

        hub = EventHub()
        transfers = [
            _transfer("t-recnosrc", "astuary", "legacy.mp3", 5000, "completed",
                      progress=100.0)
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "astuary"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "legacy.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        _source, is_rec, title, artist, category, _library, _mbid = beets.calls[0]
        assert is_rec is True
        assert title == "Legacy Track"
        assert category == "weekly_jams"

    def test_no_search_context_passes_none(self, tmp_config, db):
        """A transfer slskd reports with no matching pending/search row
        (e.g. adopted mid-flight) has nothing to resolve — beets falls
        back to matching on the file's own tags, same as pre-P-MB-1."""
        hub = EventHub()
        transfers = [
            _transfer("t-orphan", "peerone", "a\\Track.mp3", 5000, "completed",
                      progress=100.0)
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "peerone"
        )
        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        _source, _is_rec, title, artist, _category, _library, _mbid = beets.calls[0]
        assert title is None
        assert artist is None

    def test_library_download_routes_through_library_and_mbid(self, tmp_config, db):
        """P6.8: a row with is_library_download=1 is a manual download
        (is_rec=False) but must import through library=True pinned to its
        recording MBID."""
        store = DownloadStore(db)
        store.insert_pending(
            "s-library",
            "peerone",
            "a\\Track.mp3",
            5000,
            False,
            is_library_download=True,
            mb_recording_id="mbid-123",
        )

        hub = EventHub()
        transfers = [
            _transfer(
                "t-lib", "peerone", "a\\Track.mp3", 5000, "completed", progress=100.0
            )
        ]
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "peerone"
        )
        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(transfers),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        _source, is_rec, _title, _artist, _category, library, mbid = beets.calls[0]
        assert is_rec is False
        assert library is True
        assert mbid == "mbid-123"


class TestMissingSourceTimeout:
    """A transfer slskd swears is 'completed' but whose file never appears
    on disk (the dominant real case: a zombie row adopted from slskd's own
    transfer history, which outlives a musica reset, pointing at a file the
    reset already deleted) must eventually stop being retried rather than
    spinning forever — see _handle_missing_source's docstring."""

    def _completed(self, tid="t-ghost"):
        return [
            _transfer(tid, "ghost", "a\\Track.mp3", 5000, "completed", progress=100.0)
        ]

    def test_keeps_retrying_before_the_timeout(self, tmp_config, db):
        hub = EventHub()
        tmp_config.beets.enabled = True
        tmp_config.download.missing_source_timeout_minutes = 5
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)

        assert beets.calls == []
        assert not DownloadStore(db).import_handled("t-ghost")
        assert "t-ghost" in monitor._missing_source_since

    def test_gives_up_after_the_timeout(self, tmp_config, db):
        hub = EventHub()
        tmp_config.beets.enabled = True
        tmp_config.download.missing_source_timeout_minutes = 5
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )
        # Simulate having already been missing for longer than the timeout,
        # rather than sleeping in the test.
        monitor._missing_source_since["t-ghost"] = time.time() - 301

        _run_poll_and_capture(monitor, hub)

        assert beets.calls == []
        assert DownloadStore(db).import_handled("t-ghost")
        assert "t-ghost" not in monitor._missing_source_since

    def test_giving_up_stops_the_retry_even_though_slskd_still_says_completed(
        self, tmp_config, db
    ):
        """slskd never stops reporting this transfer as 'completed' (it is
        not wrong — the network transfer did complete), so the terminal
        marker must be import_skipped, not state='failed': upsert_transfer
        would silently overwrite a 'failed' state back to 'completed' on
        the very next poll and the row would start retrying all over
        again."""
        hub = EventHub()
        tmp_config.beets.enabled = True
        tmp_config.download.missing_source_timeout_minutes = 5
        beets = FakeBeetsService(tmp_config)
        monitor = DownloadMonitor(
            tmp_config,
            FakeDownloadService(self._completed()),
            FakeLibraryService(),
            db,
            hub,
            interval=15,
            beets_service=beets,
        )
        monitor._missing_source_since["t-ghost"] = time.time() - 301
        _run_poll_and_capture(monitor, hub)
        assert DownloadStore(db).import_handled("t-ghost")

        # slskd reports the exact same transfer as "completed" again.
        _run_poll_and_capture(monitor, hub)

        assert beets.calls == []

    def test_source_found_before_timeout_clears_the_tracking_entry(
        self, tmp_config, db
    ):
        """A transient miss that resolves itself (e.g. the file becomes
        visible a poll later) must not leave a stale timer running."""
        hub = EventHub()
        source_dir = (
            Path(tmp_config.paths.download_dir) / "complete" / "soulseek" / "ghost"
        )
        tmp_config.beets.enabled = True
        beets = FakeBeetsService(tmp_config)
        service = FakeDownloadService(self._completed())
        monitor = DownloadMonitor(
            tmp_config, service, FakeLibraryService(), db, hub, interval=15,
            beets_service=beets,
        )

        _run_poll_and_capture(monitor, hub)
        assert "t-ghost" in monitor._missing_source_since
        assert beets.calls == []

        (source_dir / "a").mkdir(parents=True, exist_ok=True)
        (source_dir / "a" / "Track.mp3").write_text("fake mp3 data")

        _run_poll_and_capture(monitor, hub)

        assert len(beets.calls) == 1
        assert "t-ghost" not in monitor._missing_source_since


class TestBeetsDuplicateHandling:
    """A duplicate skip is terminal, not a failure — otherwise the monitor
    re-runs the same doomed import on every poll cycle forever."""

    def _monitor(self, tmp_config, db, hub, beets):
        tmp_config.beets.enabled = True
        source = Path(
            tmp_config.paths.download_dir,
            "complete",
            "soulseek",
            "peerone",
            "a",
            "b",
            "01 - Track.mp3",
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("fake mp3 data")
        transfers = [
            _transfer(
                "t-dup", "peerone", "a\\b\\01 - Track.mp3", 5000, "completed",
                progress=100.0,
            )
        ]
        return (
            DownloadMonitor(
                tmp_config,
                FakeDownloadService(transfers),
                FakeLibraryService(),
                db,
                hub,
                interval=15,
                beets_service=beets,
            ),
            source,
        )

    def test_duplicate_is_not_retried_on_the_next_poll(self, tmp_config, db):
        from app.services.beets import BeetsImportResult

        class DuplicateBeets:
            def __init__(self):
                self.calls = 0

            def import_file(
                self,
                source,
                is_rec,
                title=None,
                artist=None,
                category=None,
                library=None,
                mbid=None,
            ):
                self.calls += 1
                return BeetsImportResult(False, None, "already in library", True)

        beets = DuplicateBeets()
        hub = EventHub()
        monitor, source = self._monitor(tmp_config, db, hub, beets)

        _run_poll_and_capture(monitor, hub)
        _run_poll_and_capture(monitor, hub)
        _run_poll_and_capture(monitor, hub)

        assert beets.calls == 1, "duplicate import was re-attempted every poll"
        assert source.exists(), "the redundant download must not be deleted"
        # Terminal, but NOT recorded as a move — the file never moved. It
        # used to be written as file_moved = 1 with an empty target_dir,
        # which is how "complete downloads disappear into thin air" looked
        # from the UI and the API (migration 007).
        store = DownloadStore(db)
        assert store.import_handled("t-dup")
        assert not store.file_moved("t-dup")
        row = db.fetch_one(
            "SELECT import_skipped, target_dir FROM downloads WHERE id = 't-dup'"
        )
        assert row["import_skipped"] == 1
        assert not row["target_dir"]

    def test_skipped_import_keeps_the_file_findable(self, tmp_config, db):
        """A skipped download must still be locatable on disk afterwards —
        the point of not claiming it was moved."""
        from app.services.beets import BeetsImportResult

        class DuplicateBeets:
            def import_file(
                self,
                source,
                is_rec,
                title=None,
                artist=None,
                category=None,
                library=None,
                mbid=None,
            ):
                return BeetsImportResult(False, None, "already in library", True)

        hub = EventHub()
        monitor, source = self._monitor(tmp_config, db, hub, DuplicateBeets())
        _run_poll_and_capture(monitor, hub)

        assert source.exists()
        row = db.fetch_one("SELECT target_dir FROM downloads WHERE id = 't-dup'")
        assert not row["target_dir"], (
            "an empty target_dir must not be paired with file_moved = 1"
        )

    def test_genuine_failure_is_still_retried(self, tmp_config, db):
        """The counterpart: a real error must NOT be marked handled, or a
        transient beets problem would permanently strand the download."""
        from app.services.beets import BeetsImportResult

        class FailingBeets:
            def __init__(self):
                self.calls = 0

            def import_file(
                self,
                source,
                is_rec,
                title=None,
                artist=None,
                category=None,
                library=None,
                mbid=None,
            ):
                self.calls += 1
                return BeetsImportResult(False, None, "beet exploded", False)

        beets = FailingBeets()
        hub = EventHub()
        monitor, _source = self._monitor(tmp_config, db, hub, beets)

        _run_poll_and_capture(monitor, hub)
        _run_poll_and_capture(monitor, hub)

        assert beets.calls == 2
        assert not DownloadStore(db).file_moved("t-dup")


class TestFileMovedBookkeeping:
    """file_moved = 1 is the app's claim that the file is at target_dir.
    Nothing may set it without one (migration 007)."""

    def test_mark_file_moved_rejects_an_empty_target_dir(self, db):
        store = DownloadStore(db)
        db.execute(
            "INSERT INTO downloads (id, username, filename, state, created_at) "
            "VALUES ('t-empty', 'peer', 'a.mp3', 'completed', 1)"
        )
        with pytest.raises(ValueError):
            store.mark_file_moved("t-empty", "")
        row = db.fetch_one("SELECT file_moved FROM downloads WHERE id = 't-empty'")
        assert not row["file_moved"]

    def test_import_handled_covers_both_terminal_states(self, db):
        store = DownloadStore(db)
        for tid in ("t-moved", "t-skipped", "t-open"):
            db.execute(
                "INSERT INTO downloads (id, username, filename, state, created_at) "
                f"VALUES ('{tid}', 'peer', 'a.mp3', 'completed', 1)"
            )
        store.mark_file_moved("t-moved", "/music/Searches/A/B")
        store.mark_import_skipped("t-skipped")

        assert store.import_handled("t-moved")
        assert store.import_handled("t-skipped")
        assert not store.import_handled("t-open")
        assert not store.import_handled("t-nonexistent")

    def test_import_pending_treats_no_row_as_not_pending(self, db):
        """import_pending is not simply `not import_handled`: a transfer_id
        musica has no downloads row for at all (e.g. slskd reporting its
        own transfer history from before a musica reset) means "not ours to
        track", not "still importing" — those are different things a caller
        surfacing progress needs to tell apart."""
        store = DownloadStore(db)
        for tid in ("t-moved", "t-skipped", "t-open"):
            db.execute(
                "INSERT INTO downloads (id, username, filename, state, created_at) "
                f"VALUES ('{tid}', 'peer', 'a.mp3', 'completed', 1)"
            )
        store.mark_file_moved("t-moved", "/music/Searches/A/B")
        store.mark_import_skipped("t-skipped")

        assert not store.import_pending("t-moved")
        assert not store.import_pending("t-skipped")
        assert store.import_pending("t-open")
        assert not store.import_pending("t-nonexistent")

    def test_skipped_row_is_not_reported_as_moved(self, db):
        store = DownloadStore(db)
        db.execute(
            "INSERT INTO downloads (id, username, filename, state, created_at) "
            "VALUES ('t-s', 'peer', 'a.mp3', 'completed', 1)"
        )
        store.mark_import_skipped("t-s")
        assert not store.file_moved("t-s")
        row = db.fetch_one("SELECT target_dir FROM downloads WHERE id = 't-s'")
        assert not row["target_dir"]
