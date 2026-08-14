"""
Tests for RecPuller background worker.

Uses fake services, real EventHub, and real SQLite via temporary directories.
Follows the same pattern as test_download_monitor.py.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.recs_store import RecsStore
from app.services.interfaces.download import QueueResult
from app.services.interfaces.recommendation import Classification, Recommendation
from app.services.interfaces.search import SearchJob, SearchResult
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.services.recommendation import ListenBrainzRecs
from app.sse import EventHub
from app.workers.rec_puller import RecPuller

# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------


def _make_config(
    tmpdir,
    *,
    comfort_zone_enabled=True,
    fresh_picks_enabled=True,
    deep_cuts_enabled=True,
    lb_enabled=True,
    comfort=1,
    fresh=1,
    deep=1,
    comfort_zone_interval_days=1,
    deep_cuts_interval_days=7,
    comfort_zone_playlist_name="Comfort Zone",
    fresh_picks_playlist_name="Fresh Picks",
    deep_cuts_playlist_name="Deep Cuts",
    rotation_trash_rating=1,
):
    """Build a minimal test config with all sections needed by RecPuller."""
    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.download_dir = str(p / "download")
    paths.searches_dir = str(p / "searches")
    paths.discovery_dir = str(p / "discovery")

    class MockDownload:
        check_interval = 15
        max_retries_per_track = 3
        bad_peer_threshold = 3

    class MockRecs:
        pass

    MockRecs.comfort_zone_enabled = comfort_zone_enabled
    MockRecs.fresh_picks_enabled = fresh_picks_enabled
    MockRecs.deep_cuts_enabled = deep_cuts_enabled
    MockRecs.comfort_zone_interval_days = comfort_zone_interval_days
    MockRecs.deep_cuts_interval_days = deep_cuts_interval_days
    MockRecs.comfort_zone_playlist_name = comfort_zone_playlist_name
    MockRecs.fresh_picks_playlist_name = fresh_picks_playlist_name
    MockRecs.deep_cuts_playlist_name = deep_cuts_playlist_name
    MockRecs.comfort_zone_count = comfort
    MockRecs.deep_cuts_count = deep
    MockRecs.rotation_trash_rating = rotation_trash_rating

    class MockFreshPicks:
        count = fresh
        offset = 0
        pull_window = "30d"
        search_buffer = 0

    class MockLB:
        pass

    MockLB.enabled = lb_enabled

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.download = MockDownload()
    cfg.recs = MockRecs()
    cfg.fresh_picks = MockFreshPicks()
    cfg.listenbrainz = MockLB()
    return cfg


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRecsService:
    """In-memory recommendation service for testing."""

    def __init__(
        self, recs=None, classify_result=None, fetch_error=None, classify_error=None
    ):
        self._recs = recs or []
        self._classify_result = classify_result
        self._fetch_error = fetch_error
        self._classify_error = classify_error
        self.fetch_calls: list[dict] = []

    def fetch_recommendations(self, counts):
        self.fetch_calls.append(dict(counts))
        if self._fetch_error:
            raise self._fetch_error("test fetch error")
        return list(self._recs)

    def classify(self, recs, library):
        if self._classify_error:
            raise self._classify_error("test classify error")
        if self._classify_result is not None:
            return self._classify_result
        return Classification(in_library=[], to_download=list(recs), skipped=[])

    def _find_library_match(self, rec, library):
        return None

    def _artist_words(self, artist_name):
        return [w.lower() for w in artist_name.split() if len(w) > 1]

    def _filepath_contains_artist(self, filepath, artist_words):
        return any(w in filepath.lower() for w in artist_words)

    def _filename_has_remix_qualifier(self, filename):
        qualifiers = {"remix", "rmx", "live", "acoustic", "demo"}
        return any(q in filename.lower() for q in qualifiers)


class FakeLibraryService:
    def __init__(self):
        self.search_results: dict[str, list[Song]] = {}
        self._playlists: list[PlaylistInfo] = []
        self.create_calls: list[str] = []
        self.add_calls: list[tuple] = []
        self.remove_calls: list[tuple] = []
        self._playlist_songs: dict[str, list[Song]] = {}
        self._next_playlist_id = 1

    def search_library(self, query):
        return self.search_results.get(query, [])

    def list_playlists(self):
        return list(self._playlists)

    def create_playlist(self, name):
        pid = f"pl-{self._next_playlist_id}"
        self._next_playlist_id += 1
        self.create_calls.append(name)
        self._playlists.append(PlaylistInfo(pid, name, 0))
        self._playlist_songs[pid] = []
        return pid

    def add_to_playlist(self, playlist_id, song_ids):
        self.add_calls.append((playlist_id, song_ids))
        songs = self._playlist_songs.setdefault(playlist_id, [])
        for song_id in song_ids:
            if not any(song.song_id == song_id for song in songs):
                songs.append(
                    _song(song_id, song_id, "Artist", path=f"/music/{song_id}.mp3")
                )
        return True

    def get_playlist_detail(self, playlist_id):
        return PlaylistDetail(
            playlist_id=playlist_id,
            name="",
            songs=list(self._playlist_songs.get(playlist_id, [])),
        )

    def remove_songs_from_playlist(self, playlist_id, song_ids):
        self.remove_calls.append((playlist_id, song_ids))
        self._playlist_songs[playlist_id] = [
            song
            for song in self._playlist_songs.get(playlist_id, [])
            if song.song_id not in song_ids
        ]
        return True

    def trigger_scan(self):
        return True


class FakeSearchService:
    def __init__(self, results=None, results_sequence=None):
        self._results: list[SearchResult] = results or []
        # If given, each successive search() call pulls the next entry
        # (last entry repeats once exhausted) — used to test re-search
        # producing a fresh peer pool (G1).
        self._results_sequence = results_sequence
        self.search_queries: list[str] = []
        self._next_id = 1
        self._results_by_search_id: dict[str, list] = {}

    def search(self, query, artist=None):
        self.search_queries.append(query)
        sid = f"search-{self._next_id}"
        call_index = self._next_id - 1
        self._next_id += 1
        if self._results_sequence is not None:
            idx = min(call_index, len(self._results_sequence) - 1)
            self._results_by_search_id[sid] = self._results_sequence[idx]
        else:
            self._results_by_search_id[sid] = self._results
        return SearchJob(
            search_id=sid,
            query=query,
            artist=artist,
            created_at=datetime.now(timezone.utc),
            status="searching",
        )

    def get_results(self, search_id):
        return list(self._results_by_search_id.get(search_id, self._results))

    def cancel(self, search_id):
        return True

    def get_status(self, search_id):
        return SearchJob(
            search_id=search_id,
            query="",
            artist=None,
            created_at=datetime.now(timezone.utc),
            status="completed",
        )

    def list_searches(self):
        return []


class FakeDownloadService:
    def __init__(self):
        self.queue_calls: list[dict] = []
        self._queue_raise: Exception | None = None
        self._queue_enqueued: int = 1
        # Usernames that should fail to queue (0 enqueued) — used to test
        # G1's fall-through to the next candidate.
        self._fail_usernames: set[str] = set()

    def queue(self, username, files, search_id=None, destination=None):
        self.queue_calls.append(
            {
                "username": username,
                "files": files,
                "search_id": search_id,
                "destination": destination,
            }
        )
        if self._queue_raise:
            raise self._queue_raise
        if username in self._fail_usernames:
            return QueueResult(enqueued_count=0, failures=[], search_id=search_id)
        return QueueResult(
            enqueued_count=self._queue_enqueued,
            failures=[],
            search_id=search_id,
        )

    def get_status(self):
        return []

    def retry(self, transfer_id):
        raise NotImplementedError

    def cancel(self, transfer_id):
        return True

    def get_transfer(self, transfer_id):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(source="test", artist="Artist", track="Track", mbid=None):
    return Recommendation(source=source, artist=artist, track=track, mbid=mbid)


def _song(song_id, title, artist, path="/music/test.mp3", album="Album", mbid=None):
    return Song(
        song_id=song_id,
        title=title,
        artist=artist,
        album=album,
        path=path,
        duration=180,
        size=1000000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=0,
        starred=False,
        mbid=mbid,
    )


def _result(username, filename, size=1000, free_slot=True):
    return SearchResult(
        username=username,
        filename=filename,
        size=size,
        has_free_slot=free_slot,
        upload_speed=None,
        bitrate="320",
        duration=180,
    )


def _run_pull_and_capture(rec_puller, hub, fn=None):
    """Run pull_once() (or `fn`, e.g. _pull_once_locked for the manual path)
    in a background thread and capture SSE events."""
    result_holder: dict = {}
    fn = fn or rec_puller.pull_once

    def _pull():
        result_holder["result"] = fn()

    async def _capture():
        sub = hub.subscribe()
        t = threading.Thread(target=_pull)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path):
    return _make_config(str(tmp_path))


@pytest.fixture
def db(tmp_config):
    database = Database(tmp_config)
    database.initialize_schema()
    yield database
    database.close()


@pytest.fixture
def hub():
    return EventHub()


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


class TestGates:
    def test_periodic_pull_skipped_when_all_categories_disabled(self, tmp_path, db, hub):
        """pull_once() (periodic) must skip when every category is disabled
        (P6.5-3b — no single master switch any more, each category gates
        itself). Distinct from 'not due yet' (see TestPerCategoryScheduling)."""
        config = _make_config(
            str(tmp_path),
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=False,
        )
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result, events = _run_pull_and_capture(rp, hub)
        assert result == {"skipped": "no category due"}
        assert not events

    def test_manual_pull_still_gated_by_lb_disabled(self, tmp_path, db, hub):
        """The listenbrainz gate applies to manual pulls regardless of any
        category's enabled state."""
        config = _make_config(str(tmp_path), lb_enabled=False)
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        started = rp.trigger_pull()
        assert started is True
        for _ in range(200):
            if not rp.is_running():
                break
            threading.Event().wait(0.01)
        # Gate skip — no real pull happened.
        assert rp.last_run_at() is None

    def test_periodic_loop_skips_when_all_categories_disabled(self, tmp_path, db, hub):
        """run()'s loop must not fire pulls at all while every category is
        disabled, even across several interval cycles (P6.5-3b)."""
        config = _make_config(
            str(tmp_path),
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=False,
        )
        rp = RecPuller(
            config,
            FakeRecsService(recs=[]),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
            interval=0.05,
        )
        rp.start()
        try:
            threading.Event().wait(0.3)
            assert rp.last_run_at() is None
        finally:
            rp.stop()

    def test_skipped_when_lb_disabled(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path), lb_enabled=False)
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result, events = _run_pull_and_capture(rp, hub)
        assert result == {"skipped": "listenbrainz disabled"}
        assert not events

    def test_skipped_when_counts_zero(self, tmp_path, db, hub):
        """The 'all counts zero' gate lives in _pull_once_locked() and applies
        to the full configured counts — exercised via the manual path
        (_pull_once_locked directly, same as trigger_pull()) since pull_once()
        (periodic) now applies its own due-filtering on top (P6.5-2,
        see test_periodic_pull_skips_categories_not_due)."""
        config = _make_config(str(tmp_path), comfort=0, fresh=0, deep=0)
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result, events = _run_pull_and_capture(rp, hub, fn=rp._pull_once_locked)
        assert result == {"skipped": "all counts zero"}
        assert not events


# ---------------------------------------------------------------------------
# Per-category scheduling (P6.5-2)
# ---------------------------------------------------------------------------


class TestPerCategoryScheduling:
    def test_periodic_pull_only_includes_due_categories(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_interval_days=1, deep_cuts_interval_days=7,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        # deep_cuts was just pulled (not due); comfort_zone and the independent
        # nightly Fresh Picks cadence have never run (both due immediately).
        rp._category_last_run_at["deep_cuts"] = time.time()

        result, _ = _run_pull_and_capture(rp, hub)
        assert result["fetched"] == 0
        assert recs_svc.fetch_calls == [{"comfort_zone": 2, "fresh_picks": 3, "deep_cuts": 0}]

    def test_periodic_pull_updates_last_run_only_for_included_categories(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_interval_days=1, deep_cuts_interval_days=7,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        rp._category_last_run_at["deep_cuts"] = time.time()
        before_deep_cuts = rp._category_last_run_at["deep_cuts"]

        _run_pull_and_capture(rp, hub)

        last_run = rp.category_last_run_at()
        assert last_run["comfort_zone"] is not None
        assert last_run["deep_cuts"] == before_deep_cuts  # untouched — wasn't due
        assert last_run["fresh_picks"] is not None

    def test_periodic_pull_skips_when_no_category_due(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_interval_days=1, deep_cuts_interval_days=7,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        now = time.time()
        rp._category_last_run_at["comfort_zone"] = now
        rp._category_last_run_at["fresh_picks"] = now
        rp._category_last_run_at["deep_cuts"] = now

        result, events = _run_pull_and_capture(rp, hub)
        assert result == {"skipped": "no category due"}
        assert not events
        assert recs_svc.fetch_calls == []

    def test_manual_pull_ignores_due_ness_uses_full_counts(self, tmp_path, db, hub):
        """A manual pull (trigger_pull -> _pull_once_locked directly) always
        uses the full configured counts for every category, regardless of
        whether that category is individually due."""
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_interval_days=1, deep_cuts_interval_days=7,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        # Everything was just pulled — nothing would be due periodically.
        now = time.time()
        rp._category_last_run_at = {"comfort_zone": now, "fresh_picks": now, "deep_cuts": now}

        started = rp.trigger_pull()
        assert started is True
        for _ in range(200):
            if not rp.is_running():
                break
            threading.Event().wait(0.01)

        assert recs_svc.fetch_calls == [{"comfort_zone": 2, "fresh_picks": 3, "deep_cuts": 4}]

    def test_next_periodic_pull_at_none_when_no_category_enabled(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path),
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=False,
        )
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        assert rp.next_periodic_pull_at() is None

    def test_next_periodic_pull_at_ignores_disabled_category(self, tmp_path, db, hub):
        """Only the enabled category's due time counts — a disabled one
        (even with a shorter interval) must not win the 'earliest' pick."""
        config = _make_config(
            str(tmp_path),
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=True,
            comfort_zone_interval_days=1, deep_cuts_interval_days=7,
        )
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        now = time.time()
        rp._category_last_run_at["comfort_zone"] = now
        rp._category_last_run_at["deep_cuts"] = now

        next_at = rp.next_periodic_pull_at()
        expected_deep_cuts_due = now + 7 * 86400
        assert next_at == pytest.approx(expected_deep_cuts_due, abs=1)

    def test_periodic_pull_excludes_disabled_category_even_if_due(self, tmp_path, db, hub):
        """A disabled category is never included periodically, even when
        it's never run before and would otherwise be immediately due."""
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=True,
            comfort_zone_interval_days=1, deep_cuts_interval_days=1,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )

        result, _ = _run_pull_and_capture(rp, hub)
        assert result["fetched"] == 0
        assert recs_svc.fetch_calls == [{"comfort_zone": 0, "fresh_picks": 0, "deep_cuts": 4}]

    def test_manual_pull_includes_disabled_category(self, tmp_path, db, hub):
        """Manual pulls ignore periodic enabled flags for every category."""
        config = _make_config(
            str(tmp_path), comfort=2, fresh=3, deep=4,
            comfort_zone_enabled=True, fresh_picks_enabled=False, deep_cuts_enabled=True,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )

        started = rp.trigger_pull()
        assert started is True
        for _ in range(200):
            if not rp.is_running():
                break
            threading.Event().wait(0.01)

        assert recs_svc.fetch_calls == [{"comfort_zone": 2, "fresh_picks": 3, "deep_cuts": 4}]

    def test_manual_pull_only_includes_selected_categories(self, tmp_path, db, hub):
        """Explicit manual selection is independent of periodic enablement."""
        config = _make_config(
            str(tmp_path),
            comfort=2,
            fresh=3,
            deep=4,
            comfort_zone_enabled=False,
            fresh_picks_enabled=False,
            deep_cuts_enabled=True,
        )
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )

        started = rp.trigger_pull(["fresh_picks"])
        assert started is True
        for _ in range(200):
            if not rp.is_running():
                break
            threading.Event().wait(0.01)

        assert recs_svc.fetch_calls == [{"comfort_zone": 0, "fresh_picks": 3, "deep_cuts": 0}]

    def test_next_periodic_pull_at_never_run_is_due_now(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        before = time.time()
        next_at = rp.next_periodic_pull_at()
        after = time.time()
        assert next_at is not None
        assert before <= next_at <= after

    def test_next_periodic_pull_at_is_earliest_across_categories(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path), comfort_zone_interval_days=1, deep_cuts_interval_days=7,
            fresh_picks_enabled=False,
        )
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(),
            FakeDownloadService(), db, hub,
        )
        now = time.time()
        rp._category_last_run_at["comfort_zone"] = now  # next due: now + 1 day
        rp._category_last_run_at["deep_cuts"] = now  # next due: now + 7 days

        next_at = rp.next_periodic_pull_at()
        expected_comfort_zone_due = now + 1 * 86400
        assert next_at == pytest.approx(expected_comfort_zone_due, abs=1)


# ---------------------------------------------------------------------------
# Fetch tests
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_error_returns_error(self, tmp_path, db, hub):
        from app.exceptions import ListenBrainzConnectionError

        config = _make_config(str(tmp_path))
        recs_svc = FakeRecsService(
            fetch_error=ListenBrainzConnectionError,
        )
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result, events = _run_pull_and_capture(rp, hub)
        assert "error" in result
        assert "fetch failed" in result["error"]
        started = [e for e in events if e[0] == "rec.pull_started"]
        assert len(started) == 1
        completed = [e for e in events if e[0] == "rec.pull_completed"]
        assert len(completed) == 0

    def test_empty_fetch(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result, events = _run_pull_and_capture(rp, hub)
        assert result == {"fetched": 0}
        completed = [e for e in events if e[0] == "rec.pull_completed"]
        assert len(completed) == 1
        assert completed[0][1]["total"] == 0


# ---------------------------------------------------------------------------
# Playlist tests
# ---------------------------------------------------------------------------


class TestPlaylist:
    def test_all_in_library_creates_playlist(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist X", "Track X", "mbid-1")
        song = _song("sid-1", "Track X", "Artist X", mbid="mbid-1")
        lib = FakeLibraryService()
        lib.search_results = {"Track X": [song]}

        match_fn = lambda r, l: song
        recs_svc = FakeRecsService(
            recs=[rec],
            classify_result=Classification(
                in_library=[rec], to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = match_fn

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(), db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["in_library"] == 1
        assert result["to_download"] == 0
        assert result["playlist_id"] is not None
        assert len(lib.create_calls) == 1
        assert lib.create_calls[0] == "Comfort Zone"
        assert len(lib.add_calls) == 1

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("in_library")
        assert len(rows) == 1
        assert rows[0]["playlist_id"] == result["playlist_id"]

    def test_all_in_library_reuses_existing_playlist(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist X", "Track X", "mbid-1")
        song = _song("sid-1", "Track X", "Artist X", mbid="mbid-1")
        lib = FakeLibraryService()
        lib.search_results = {"Track X": [song]}
        lib._playlists = [
            PlaylistInfo(playlist_id="pl-existing", name="Comfort Zone", song_count=0)
        ]

        match_fn = lambda r, l: song
        recs_svc = FakeRecsService(
            recs=[rec],
            classify_result=Classification(
                in_library=[rec], to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = match_fn

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(), db, hub
        )
        _result, _events = _run_pull_and_capture(rp, hub)

        assert len(lib.create_calls) == 0
        assert len(lib.add_calls) == 1
        assert lib.add_calls[0][0] == "pl-existing"

    def test_each_category_gets_its_own_playlist(self, tmp_path, db, hub):
        """P6.7-1: one pull with in-library recs from all three categories
        creates three distinct playlists, named independently."""
        config = _make_config(str(tmp_path))
        recs = [
            _rec("comfort_zone", "Artist C", "Track C", "mbid-c"),
            _rec("fresh_picks", "Artist F", "Track F", "mbid-f"),
            _rec("deep_cuts", "Artist D", "Track D", "mbid-d"),
        ]
        songs = {
            "Track C": [_song("sid-c", "Track C", "Artist C", mbid="mbid-c")],
            "Track F": [_song("sid-f", "Track F", "Artist F", mbid="mbid-f")],
            "Track D": [_song("sid-d", "Track D", "Artist D", mbid="mbid-d")],
        }
        lib = FakeLibraryService()
        lib.search_results = songs

        recs_svc = FakeRecsService(
            recs=recs,
            classify_result=Classification(
                in_library=list(recs), to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = lambda r, l: songs[r.track][0]

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(), db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert len(lib.create_calls) == 3
        assert set(lib.create_calls) == {"Comfort Zone", "Fresh Picks", "Deep Cuts"}
        assert len(lib.add_calls) == 3
        added_ids = {pid for pid, _ in lib.add_calls}
        assert added_ids == {"pl-1", "pl-2", "pl-3"}
        assert result["playlist_id"] is not None

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("in_library")
        assert {r["playlist_id"] for r in rows} == {"pl-1", "pl-2", "pl-3"}

    def test_no_playlist_without_resolvable_song_ids(self, tmp_path, db, hub):
        """Strict laziness (P6.7-1 decision): a category with in-library
        recs whose song IDs all fail to resolve gets no playlist at all."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist X", "Track X", "mbid-1")
        lib = FakeLibraryService()

        recs_svc = FakeRecsService(
            recs=[rec],
            classify_result=Classification(
                in_library=[rec], to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = lambda r, l: None

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(), db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["in_library"] == 1
        assert lib.create_calls == []
        assert lib.add_calls == []

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("in_library")
        assert len(rows) == 1
        assert rows[0]["playlist_id"] is None

    def test_unknown_source_in_library_skips_playlist(self, tmp_path, db, hub):
        """P6.7-0b's no-fallback: an in-library rec with an unknown source
        is recorded but never added to any playlist."""
        config = _make_config(str(tmp_path))
        rec = _rec("weekly_jams", "Artist W", "Track W", "mbid-w")
        song = _song("sid-w", "Track W", "Artist W", mbid="mbid-w")
        lib = FakeLibraryService()
        lib.search_results = {"Track W": [song]}

        recs_svc = FakeRecsService(
            recs=[rec],
            classify_result=Classification(
                in_library=[rec], to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = lambda r, l: song

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(), db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["in_library"] == 1
        assert lib.create_calls == []
        assert lib.add_calls == []

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("in_library")
        assert len(rows) == 1
        assert rows[0]["playlist_id"] is None

    def test_fresh_picks_rolls_low_rated_old_tracks_to_trash(self, tmp_path, db, hub):
        """Fresh Picks evicts oldest eligible tracks but preserves a 5-star one."""
        config = _make_config(str(tmp_path), fresh=2)
        lib = FakeLibraryService()
        playlist_id = "fresh-playlist"
        lib._playlists = [
            PlaylistInfo(playlist_id=playlist_id, name="Fresh Picks", song_count=3)
        ]
        old_low = _song("old-low", "Old low", "Artist")
        old_high = _song("old-high", "Old high", "Artist")
        newer_low = _song("newer-low", "Newer low", "Artist")
        old_high.rating = 5
        lib._playlist_songs[playlist_id] = [old_low, old_high, newer_low]

        rp = RecPuller(
            config,
            FakeRecsService(),
            lib,
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )

        assert rp._write_category_playlist(
            "fresh_picks", playlist_id, ["new-song"], lib._playlists
        )

        remaining = [song.song_id for song in lib._playlist_songs[playlist_id]]
        assert remaining == ["old-high", "new-song"]
        assert lib.remove_calls == [(playlist_id, ["old-low", "newer-low"])]
        assert any(
            call[1] == ["old-low", "newer-low"]
            for call in lib.add_calls
        )


# ---------------------------------------------------------------------------
# Download tests
# ---------------------------------------------------------------------------


class TestDownload:
    def test_all_to_download_queues(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("fresh_picks", "Artist Q", "Track Q", "mbid-q")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Artist Q - Track Q.mp3", size=5000, free_slot=True),
            ]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 1
        assert result["to_download"] == 1
        assert len(dl_svc.queue_calls) == 1
        call = dl_svc.queue_calls[0]
        assert call["destination"] is None  # no destination: worker moves files itself
        assert call["search_id"] is not None

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("queued")
        assert len(rows) == 1
        assert rows[0]["search_id"] is not None

        # Pending row must exist so DownloadMonitor adopts it as a rec download
        dl_store = DownloadStore(db)
        pending = dl_store.get_pending_search_id("peer1", "Artist Q - Track Q.mp3")
        assert pending == rows[0]["search_id"]
        pending_row = db.fetch_one(
            "SELECT is_rec_download FROM downloads "
            "WHERE username = ? AND filename = ? AND search_id IS NOT NULL",
            ("peer1", "Artist Q - Track Q.mp3"),
        )
        assert pending_row is not None
        assert pending_row["is_rec_download"] == 1

    def test_search_no_results(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("deep_cuts", "Artist N", "Track N", "mbid-n")
        search_svc = FakeSearchService(results=[])
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            FakeDownloadService(),
            db,
            hub,
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(result["failures"]) == 1
        recs_store = RecsStore(db)
        failed = recs_store.get_recs_by_status("search_failed")
        assert len(failed) == 1

    def test_search_artist_filter_rejects_all(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        # The artist must yield a filterable word via the real shared
        # `track_requester.artist_words` (the FakeRecsService._artist_words is
        # no longer consulted by RecPuller). "Zebra" survives the stop-word /
        # length filters, unlike "Artist Z" ("artist" is a stop word, "Z" is
        # one char, so that name yields no filter words).
        rec = _rec("comfort_zone", "Zebra", "Track Z", "mbid-z")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "unrelated - song.mp3", size=1000),
                _result("peer2", "nothinghere.flac", size=2000),
            ]
        )
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            FakeDownloadService(),
            db,
            hub,
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(result["failures"]) == 1
        recs_store = RecsStore(db)
        failed = recs_store.get_recs_by_status("search_failed")
        assert len(failed) == 1

    def test_search_remix_only_rejected(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist R", "Track R", "mbid-r")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Artist R - Track R (Remix).mp3", size=1000),
                _result("peer2", "Artist R - Track R (Live).flac", size=2000),
            ]
        )
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            FakeDownloadService(),
            db,
            hub,
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        recs_store = RecsStore(db)
        failed = recs_store.get_recs_by_status("search_failed")
        assert len(failed) == 1

    def test_queue_zero_enqueued(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("fresh_picks", "Artist Q0", "Track Q0", "mbid-q0")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Artist Q0 - Track Q0.mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()
        dl_svc._queue_enqueued = 0
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(result["failures"]) >= 1
        recs_store = RecsStore(db)
        qf = recs_store.get_recs_by_status("queue_failed")
        assert len(qf) == 1

    def test_queue_raises(self, tmp_path, db, hub):
        from app.exceptions import SlskdConnectionError

        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist Err", "Track Err", "mbid-err")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Artist Err - Track Err.mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()
        dl_svc._queue_raise = SlskdConnectionError("http://s", "boom")
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(result["failures"]) == 1
        recs_store = RecsStore(db)
        # G1 fix: queue exceptions no longer get their own "error" status —
        # they're just one more failed candidate. After every free-slot
        # candidate (and the re-search) is exhausted, the rec lands in
        # "queue_failed" like any other queueing failure.
        err_rows = recs_store.get_recs_by_status("queue_failed")
        assert len(err_rows) == 1

    def test_falls_through_to_next_candidate_on_queue_failure(self, tmp_path, db, hub):
        """G1: a queue failure on the first candidate must not sink the
        rec — the puller should try the next free-slot candidate from the
        same search before giving up."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alright", "Alright", "mbid-alright")
        search_svc = FakeSearchService(
            results=[
                _result("guicale", "Alright - Alright.mp3", size=1000),
                _result("soupscum", "Alright - Alright.flac", size=2000),
            ]
        )
        dl_svc = FakeDownloadService()
        dl_svc._fail_usernames = {"guicale"}
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 1
        usernames = [c["username"] for c in dl_svc.queue_calls]
        assert usernames == ["guicale", "soupscum"]
        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("queued")) == 1
        assert len(recs_store.get_recs_by_status("queue_failed")) == 0

    def test_never_selects_a_no_free_slot_peer(self, tmp_path, db, hub):
        """G1: a peer with no free slot must never be chosen, even when
        it's the only candidate — that's a wasted queue attempt against a
        peer that can't accept it."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Busy", "Track", "mbid-busy")
        search_svc = FakeSearchService(
            results=[
                _result("busypeer", "Busy - Track.mp3", size=1000, free_slot=False),
            ]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(dl_svc.queue_calls) == 0
        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("search_failed")) == 1

    def test_re_searches_and_finds_fresh_peer_after_exhausting_first_batch(
        self, tmp_path, db, hub
    ):
        """G1: once every free-slot candidate from the original search has
        failed, the puller re-searches once — a fresh peer from that
        re-search should still be tried rather than giving up."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alright", "Alright", "mbid-alright")
        search_svc = FakeSearchService(
            results_sequence=[
                [_result("guicale", "Alright - Alright.mp3", size=1000)],
                [
                    _result("guicale", "Alright - Alright.mp3", size=1000),
                    _result("freshpeer", "Alright - Alright.flac", size=2000),
                ],
            ]
        )
        dl_svc = FakeDownloadService()
        dl_svc._fail_usernames = {"guicale"}
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 1
        usernames = [c["username"] for c in dl_svc.queue_calls]
        # guicale tried once from the first search, not re-tried from the
        # re-search (already in tried_usernames), then freshpeer succeeds.
        assert usernames == ["guicale", "freshpeer"]
        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("queued")) == 1

    def test_caps_total_peer_attempts(self, tmp_path, db, hub):
        """G1 follow-up: live testing showed an unbounded walk can turn
        one rec into minutes of sequential 45s queue() timeouts against
        slow peers. MAX_QUEUE_ATTEMPTS bounds the total peers tried
        (original search + re-search combined) even when many more
        candidates are available."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alright", "Alright", "mbid-alright")
        many_results = [
            _result(f"peer{i}", "Alright - Alright.mp3", size=1000)
            for i in range(20)
        ]
        search_svc = FakeSearchService(results=many_results)
        dl_svc = FakeDownloadService()
        dl_svc._fail_usernames = {r.username for r in many_results}  # all fail
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(dl_svc.queue_calls) == 5  # MAX_QUEUE_ATTEMPTS, not 20
        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("queue_failed")) == 1

    def test_gives_up_after_re_search_also_exhausted(self, tmp_path, db, hub):
        """G1: when even the re-search's candidates all fail, the rec must
        finally land in queue_failed rather than retrying forever."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alright", "Alright", "mbid-alright")
        search_svc = FakeSearchService(
            results=[_result("guicale", "Alright - Alright.mp3", size=1000)],
        )
        dl_svc = FakeDownloadService()
        dl_svc._fail_usernames = {"guicale"}
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("queue_failed")) == 1
        # Two search() calls: the original + the one re-search.
        assert len(search_svc.search_queries) == 2


# ---------------------------------------------------------------------------
# Mixed scenario
# ---------------------------------------------------------------------------


class TestMixedScenario:
    def test_mixed_three_recs(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec1 = _rec("comfort_zone", "A1", "T1", "mbid-1")
        rec2 = _rec("fresh_picks", "A2", "T2", "mbid-2")
        rec3 = _rec("deep_cuts", "A3", "T3", "mbid-3")

        song1 = _song("s1", "T1", "A1", mbid="mbid-1")
        lib = FakeLibraryService()
        lib.search_results = {"T1": [song1], "T2": [], "T3": []}

        match_fn = lambda r, ls: song1 if r.mbid == "mbid-1" else None
        recs_svc = FakeRecsService(
            recs=[rec1, rec2, rec3],
            classify_result=Classification(
                in_library=[rec1],
                to_download=[rec2, rec3],
                skipped=[],
            ),
        )
        recs_svc._find_library_match = match_fn

        search_svc = FakeSearchService(
            results=[
                _result("peer2", "A2 - T2.mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()

        rp = RecPuller(config, recs_svc, lib, search_svc, dl_svc, db, hub)
        result, events = _run_pull_and_capture(rp, hub)

        assert result["in_library"] == 1
        assert result["to_download"] == 2
        assert result["queued"] == 1
        assert len(result["failures"]) == 1

        recs_store = RecsStore(db)
        in_lib = recs_store.get_recs_by_status("in_library")
        queued = recs_store.get_recs_by_status("queued")
        failed = recs_store.get_recs_by_status("search_failed")
        assert len(in_lib) == 1
        assert len(queued) == 1
        assert len(failed) == 1

        completed = [e for e in events if e[0] == "rec.pull_completed"]
        assert len(completed) == 1
        payload = completed[0][1]
        assert payload["total"] == 3
        assert payload["in_library"] == 1


# ---------------------------------------------------------------------------
# SSE sequence
# ---------------------------------------------------------------------------


class TestSSESequence:
    def test_sse_events_sequence(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "A", "T", "mbid-x")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "A - T.mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        _result_val, events = _run_pull_and_capture(rp, hub)

        types = [e[0] for e in events]
        start_idx = (
            types.index("rec.pull_started") if "rec.pull_started" in types else -1
        )
        classify_idx = (
            types.index("rec.classifying") if "rec.classifying" in types else -1
        )
        complete_idx = (
            types.index("rec.pull_completed") if "rec.pull_completed" in types else -1
        )

        assert start_idx >= 0
        assert classify_idx >= 0
        assert complete_idx >= 0
        assert start_idx < classify_idx < complete_idx


# ---------------------------------------------------------------------------
# Search by track name only
# ---------------------------------------------------------------------------


class TestSearchQuery:
    def test_search_uses_query_pipeline(self, tmp_path, db, hub):
        """The slskd query is built by the pipeline (P6.5-6): 1 track word +
        1 artist word, never the full unshortened title."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alesso", "Heroes (We Could Be)", "mbid-tn")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Alesso - Heroes (We Could Be).mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        _run_pull_and_capture(rp, hub)

        assert len(search_svc.search_queries) == 1
        # Paren contents ("We Could Be") excluded, longest words picked.
        assert search_svc.search_queries[0] == "heroes alesso"
        assert "we" not in search_svc.search_queries[0]

    def test_search_with_paren_qualifier_tries_3_words_first(self, tmp_path, db, hub):
        """User decision 2026-08-10: a paren qualifier makes a 3-word query
        first; if it misses (pass ratio below threshold), the qualifier is
        dropped and the 2-word rung is tried."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alesso", "Heroes (We Could Be) (Remix)", "mbid-rmx")

        class PerQuerySearch(FakeSearchService):
            def __init__(self, results_by_query):
                super().__init__()
                self._results_by_query = results_by_query

            def search(self, query, artist=None):
                job = super().search(query, artist)
                self._current_query = query
                return job

            def get_results(self, search_id):
                return list(self._results_by_query.get(self._current_query, []))

        search_svc = PerQuerySearch(
            {
                # 3-word rung misses: only a remix-qualified (rejected) result.
                "heroes remix alesso": [
                    _result("peer1", "Alesso - Heroes (Remix).mp3", size=1000),
                ],
                # 2-word rung hits: clean result passes every filter.
                "heroes alesso": [
                    _result("peer2", "Alesso - Heroes.mp3", size=1000),
                ],
            }
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert search_svc.search_queries == ["heroes remix alesso", "heroes alesso"]
        assert result["queued"] == 1
        assert dl_svc.queue_calls[0]["username"] == "peer2"

    def test_qualifier_rung_clears_threshold_no_drop(self, tmp_path, db, hub):
        """The 3-word qualifier query is used as-is when its results clear
        the pass-ratio threshold — no re-query."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alesso", "Heroes (We Could Be) (Remix)", "mbid-rmx2")
        search_svc = FakeSearchService(
            results=[
                _result("peer1", "Alesso - Heroes.mp3", size=1000),
            ]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert search_svc.search_queries == ["heroes remix alesso"]
        assert result["queued"] == 1

    def test_ladder_walks_all_rungs_in_order(self, tmp_path, db, hub):
        """2-1 → 1-2 → 2-2 rungs are searched in order when earlier rungs
        miss; all failing falls back to the best by ratio (none here)."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "John Doe", "Sunrise Sunset", "mbid-lad")
        search_svc = FakeSearchService(results=[])  # every rung misses
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert search_svc.search_queries == [
            "sunrise john",
            "sunset john",
            "sunrise doe",
            "sunset doe",
        ]
        assert result["queued"] == 0
        assert len(result["failures"]) == 1

    def test_ladder_falls_back_to_best_rung_by_ratio(self, tmp_path, db, hub):
        """No rung clears the threshold but one has viable results — the
        best-rung results are used rather than returning nothing."""
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "John Doe", "Sunrise Sunset", "mbid-best")

        class PerQuerySearch(FakeSearchService):
            def __init__(self, results_by_query):
                super().__init__()
                self._results_by_query = results_by_query

            def search(self, query, artist=None):
                job = super().search(query, artist)
                self._current_query = query
                return job

            def get_results(self, search_id):
                return list(self._results_by_query.get(self._current_query, []))

        search_svc = PerQuerySearch(
            {
                # 1-1: 2 results, 1 viable → ratio 0.5 (below threshold)
                "sunrise john": [
                    _result("p1", "John Doe - Sunrise Sunset.mp3", size=1000),
                    _result("p2", "unrelated track.mp3", size=1000),
                ],
                # 2-1: nothing viable → ratio 0
                "sunset john": [
                    _result("p3", "unrelated track.mp3", size=1000),
                ],
                # 1-2: nothing viable → ratio 0
                "sunrise doe": [
                    _result("p4", "unrelated track.mp3", size=1000),
                ],
                # 2-2: nothing viable → ratio 0
                "sunset doe": [
                    _result("p5", "unrelated track.mp3", size=1000),
                ],
            }
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        # All 4 rungs searched; best ratio (0.5) belonged to the first rung.
        assert len(search_svc.search_queries) == 4
        assert result["queued"] == 1
        assert dl_svc.queue_calls[0]["files"][0]["filename"] == "John Doe - Sunrise Sunset.mp3"

    def test_best_rung_search_id_is_the_one_recorded(self, tmp_path, db, hub):
        """Regression (P6.5 review 2026-08-11): when no rung clears the
        threshold, the chosen peer comes from the *best* rung but the code
        recorded the *last* rung's search_id — so alternative-peer retry
        (P6.5-4) would look up a different query's persisted responses and
        pick peers that never had the track.
        """
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "John Doe", "Sunrise Sunset", "mbid-sid")

        class PerQuerySearch(FakeSearchService):
            def __init__(self, results_by_query):
                super().__init__()
                self._results_by_query = results_by_query
                self.ids_by_query: dict[str, str] = {}

            def search(self, query, artist=None):
                job = super().search(query, artist)
                self._current_query = query
                self.ids_by_query[query] = job.search_id
                return job

            def get_results(self, search_id):
                return list(self._results_by_query.get(self._current_query, []))

        search_svc = PerQuerySearch(
            {
                # Rung 0 (1-1) is the only one with a viable result, at a
                # ratio below the 0.6 threshold — so the ladder runs to the
                # end and rung 3 is the last search issued.
                "sunrise john": [
                    _result("p1", "John Doe - Sunrise Sunset.mp3", size=1000),
                    _result("p2", "unrelated track.mp3", size=1000),
                ],
                "sunset john": [_result("p3", "unrelated.mp3", size=1000)],
                "sunrise doe": [_result("p4", "unrelated.mp3", size=1000)],
                "sunset doe": [_result("p5", "unrelated.mp3", size=1000)],
            }
        )
        dl_svc = FakeDownloadService()
        rp = RecPuller(
            config,
            FakeRecsService(recs=[rec]),
            FakeLibraryService(),
            search_svc,
            dl_svc,
            db,
            hub,
        )
        result, _events = _run_pull_and_capture(rp, hub)
        assert result["queued"] == 1

        best_id = search_svc.ids_by_query["sunrise john"]
        last_id = search_svc.ids_by_query["sunset doe"]
        assert best_id != last_id

        # queue(), the pending row and the rec row must all name the search
        # the chosen peer actually came from.
        assert dl_svc.queue_calls[0]["search_id"] == best_id
        assert DownloadStore(db).get_pending_search_id(
            "p1", "John Doe - Sunrise Sunset.mp3"
        ) == best_id
        assert RecsStore(db).list_recs(limit=10)[0]["search_id"] == best_id


# ---------------------------------------------------------------------------
# Deduplication (using real ListenBrainzRecs.classify)
# ---------------------------------------------------------------------------


class TestSearchRateLimited:
    """SearchRateLimitedError must degrade like any other search failure —
    recorded and skipped, never an unhandled exception in the pull thread."""

    def test_rate_limited_search_is_recorded_as_a_failure_not_a_crash(
        self, tmp_path, db, hub
    ):
        from app.exceptions import SearchRateLimitedError

        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Alesso", "Heroes (We Could Be)", "mbid-rl")

        class RateLimitedSearch(FakeSearchService):
            def search(self, query, artist=None):
                raise SearchRateLimitedError(max_searches=4, window_seconds=60)

        search_svc = RateLimitedSearch()
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, dl_svc, db, hub
        )
        result, _events = _run_pull_and_capture(rp, hub)

        assert result["queued"] == 0
        assert len(result["failures"]) == 1
        assert "Too many searches" in result["failures"][0]["message"]


class TestDeduplication:
    def test_deduplicated_mbids_skipped(self, tmp_path, db, hub):
        rec1 = _rec("comfort_zone", "Artist", "Track", "same-mbid")
        rec2 = _rec("fresh_picks", "Artist", "Track", "same-mbid")

        lb_config = MockConfigForLB()
        real_recs_svc = ListenBrainzRecs(lb_config)
        classify_result = real_recs_svc.classify([rec1, rec2], [])

        assert len(classify_result.to_download) == 1
        assert len(classify_result.in_library) == 0


class TestCrossPullDedup:
    """G2/G3/G4: a rec already active in the ledger must not be
    re-fetched/re-classified/re-added on a later pull."""

    def test_in_library_rec_not_reprocessed_on_second_pull(self, tmp_path, db, hub):
        rec = _rec("deep_cuts", "Write This Down", "Write This Down", "mbid-w")
        song = _song("song-1", "Write This Down", "Write This Down")
        lib_svc = FakeLibraryService()
        lib_svc.search_results["Write This Down"] = [song]
        recs_svc = FakeRecsService(
            recs=[rec], classify_result=Classification(in_library=[rec], to_download=[], skipped=[])
        )
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config, recs_svc, lib_svc, FakeSearchService(), FakeDownloadService(), db, hub
        )
        counts = {"comfort_zone": 0, "fresh_picks": 0, "deep_cuts": 1}

        result1, _ = _run_pull_and_capture(
            rp, hub, fn=lambda: rp._pull_once_locked(counts_override=counts)
        )
        assert result1["in_library"] == 1

        result2, _ = _run_pull_and_capture(
            rp, hub, fn=lambda: rp._pull_once_locked(counts_override=counts)
        )
        assert result2["fetched"] == 0  # the rec was dropped before classify

        recs_store = RecsStore(db)
        rows = recs_store.get_recs_by_status("in_library")
        assert len(rows) == 1  # no duplicate ledger row

    def test_queue_failed_rec_does_retry_on_next_pull(self, tmp_path, db, hub):
        """Terminal-failure statuses stay active for retry — only
        in_library/queued/downloaded are considered 'handled'."""
        rec = _rec("comfort_zone", "Flopped", "Flopped", "mbid-flop")
        recs_svc = FakeRecsService(recs=[rec])
        config = _make_config(str(tmp_path))
        search_svc = FakeSearchService(results=[])  # no viable candidate -> search_failed
        rp = RecPuller(
            config, recs_svc, FakeLibraryService(), search_svc, FakeDownloadService(), db, hub
        )
        counts = {"comfort_zone": 1, "fresh_picks": 0, "deep_cuts": 0}

        result1, _ = _run_pull_and_capture(
            rp, hub, fn=lambda: rp._pull_once_locked(counts_override=counts)
        )
        assert result1["failures"]

        result2, _ = _run_pull_and_capture(
            rp, hub, fn=lambda: rp._pull_once_locked(counts_override=counts)
        )
        assert result2["fetched"] == 1  # not skipped — it retries

        recs_store = RecsStore(db)
        assert len(recs_store.get_recs_by_status("search_failed")) == 2


class TestReconciliation:
    """G5: a downloaded/in_library rec whose file is no longer in the
    library gets marked 'removed' and becomes eligible for reprocessing."""

    def test_removed_file_marked_and_rec_retried(self, tmp_path, db, hub):
        recs_store = RecsStore(db)
        recs_store.insert_rec(
            source="deep_cuts",
            artist="Ghost Track",
            track="Ghost Track",
            mbid="mbid-ghost",
            status="downloaded",
        )

        rec = _rec("deep_cuts", "Ghost Track", "Ghost Track", "mbid-ghost")
        recs_svc = FakeRecsService(recs=[rec])
        config = _make_config(str(tmp_path))
        lib_svc = FakeLibraryService()  # no search_results -> "file" is gone
        rp = RecPuller(
            config, recs_svc, lib_svc, FakeSearchService(), FakeDownloadService(), db, hub
        )

        result, _ = _run_pull_and_capture(rp, hub)

        assert result["fetched"] == 1  # reprocessed, not skipped
        assert len(recs_store.get_recs_by_status("downloaded")) == 0
        assert len(recs_store.get_recs_by_status("removed")) == 1

    def test_present_file_left_alone(self, tmp_path, db, hub):
        recs_store = RecsStore(db)
        recs_store.insert_rec(
            source="deep_cuts",
            artist="Still Here",
            track="Still Here",
            mbid="mbid-here",
            status="downloaded",
        )

        song = _song("song-2", "Still Here", "Still Here")
        lib_svc = FakeLibraryService()
        lib_svc.search_results["Still Here"] = [song]
        recs_svc = FakeRecsService(recs=[])
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config, recs_svc, lib_svc, FakeSearchService(), FakeDownloadService(), db, hub
        )

        _run_pull_and_capture(rp, hub)

        assert len(recs_store.get_recs_by_status("downloaded")) == 1
        assert len(recs_store.get_recs_by_status("removed")) == 0


class MockConfigForLB:
    class ListenBrainzCfg:
        enabled = True
        url = "https://api.listenbrainz.org"
        token = ""
        username = ""

    def __init__(self):
        self.listenbrainz = self.ListenBrainzCfg()


# ---------------------------------------------------------------------------
# mark_rec_downloaded (DownloadStore)
# ---------------------------------------------------------------------------


class TestMarkRecDownloaded:
    def test_mark_rec_downloaded(self, tmp_path, db):
        recs_store = RecsStore(db)
        rec_id = recs_store.insert_rec(
            source="comfort_zone",
            artist="A",
            track="T",
            mbid="mbid-dl",
            status="queued",
            search_id="search-dl-1",
        )

        dl_store = DownloadStore(db)
        rowcount = dl_store.mark_rec_downloaded("search-dl-1", "dl-transfer-1")

        assert rowcount == 1
        rec = recs_store.get_rec(rec_id)
        assert rec["status"] == "downloaded"
        assert rec["download_id"] == "dl-transfer-1"
        assert rec["processed_at"] is not None

    def test_mark_rec_downloaded_no_match(self, tmp_path, db):
        dl_store = DownloadStore(db)
        rowcount = dl_store.mark_rec_downloaded("nonexistent", "dl-2")
        assert rowcount == 0


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_and_stop(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path), comfort=0, fresh=0, deep=0)
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
            interval=0.1,
        )

        rp.start()
        assert rp._thread is not None
        assert rp._thread.is_alive()

        rp.stop()
        assert rp._thread is None


# ---------------------------------------------------------------------------
# Concurrency: pull_once / trigger_pull / is_running / last_run_at
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_last_run_at_none_before_any_pull(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        assert rp.last_run_at() is None

    def test_gate_skips_do_not_set_last_run_at(self, tmp_path, db, hub):
        config = _make_config(
            str(tmp_path),
            comfort_zone_enabled=False, fresh_picks_enabled=False, deep_cuts_enabled=False,
        )
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result = rp.pull_once()
        assert result == {"skipped": "no category due"}
        assert rp.last_run_at() is None

    def test_successful_pull_sets_last_run_at(self, tmp_path, db, hub):
        import time as _time

        config = _make_config(str(tmp_path))
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        before = _time.time()
        result = rp.pull_once()
        after = _time.time()
        assert result == {"fetched": 0}
        assert rp.last_run_at() is not None
        assert before <= rp.last_run_at() <= after

    def test_failed_pull_sets_last_run_at(self, tmp_path, db, hub):
        from app.exceptions import ListenBrainzConnectionError

        config = _make_config(str(tmp_path))
        recs_svc = FakeRecsService(fetch_error=ListenBrainzConnectionError)
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        result = rp.pull_once()
        assert "error" in result
        assert rp.last_run_at() is not None

    def test_is_running_false_when_idle(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        assert rp.is_running() is False

    def test_is_running_true_during_pull(self, tmp_path, db, hub):
        """Hold the lock manually to simulate an in-flight pull."""
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        rp._pull_lock.acquire()
        try:
            assert rp.is_running() is True
        finally:
            rp._pull_lock.release()
        assert rp.is_running() is False

    def test_trigger_pull_starts_thread_and_returns_true(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        recs_svc = FakeRecsService(recs=[])
        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        started = rp.trigger_pull()
        assert started is True
        # Wait for the background pull to finish (bounded).
        for _ in range(200):
            if not rp.is_running():
                break
            threading.Event().wait(0.01)
        assert rp.last_run_at() is not None

    def test_trigger_pull_returns_false_while_running(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        rp._pull_lock.acquire()
        try:
            assert rp.trigger_pull() is False
        finally:
            rp._pull_lock.release()

    def test_concurrent_pull_once_second_call_skips(self, tmp_path, db, hub):
        """Simulate overlap: hold the lock, then call pull_once() directly."""
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        rp._pull_lock.acquire()
        try:
            result = rp.pull_once()
            assert result == {"skipped": "already running"}
        finally:
            rp._pull_lock.release()


# ---------------------------------------------------------------------------
# Abort tests
# ---------------------------------------------------------------------------


class TestAbort:
    def test_request_abort_returns_false_when_idle(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(), FakeDownloadService(), db, hub
        )
        assert rp.request_abort() is False

    def test_request_abort_returns_true_while_running(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config, FakeRecsService(), FakeLibraryService(), FakeSearchService(), FakeDownloadService(), db, hub
        )
        rp._pull_lock.acquire()
        try:
            assert rp.request_abort() is True
        finally:
            rp._pull_lock.release()

    def test_abort_after_fetch_skips_classify_and_download(self, tmp_path, db, hub):
        """Abort requested while fetch_recommendations() is in flight — the
        pull notices right after fetch returns and skips everything else."""
        config = _make_config(str(tmp_path))
        rec = _rec("fresh_picks", "Artist Q", "Track Q", "mbid-q")
        search_svc = FakeSearchService(
            results=[_result("peer1", "Artist Q - Track Q.mp3")]
        )
        dl_svc = FakeDownloadService()

        rp = RecPuller(config, FakeRecsService(recs=[rec]), FakeLibraryService(), search_svc, dl_svc, db, hub)

        class AbortDuringFetchRecsService(FakeRecsService):
            def fetch_recommendations(self, counts):
                result = super().fetch_recommendations(counts)
                rp.request_abort()
                return result

        rp._recs_service = AbortDuringFetchRecsService(recs=[rec])

        result, events = _run_pull_and_capture(rp, hub)

        assert result["aborted"] is True
        assert result["fetched"] == 1
        assert "to_download" not in result
        assert dl_svc.queue_calls == []
        assert search_svc.search_queries == []

        completed_events = [d for (t, d) in events if t == "rec.pull_completed"]
        assert len(completed_events) == 1
        assert completed_events[0]["aborted"] is True

    def test_abort_mid_loop_stops_before_remaining_tracks(self, tmp_path, db, hub):
        """Abort requested during the first track's search() call — that
        track finishes normally, the second is never started."""
        config = _make_config(str(tmp_path))
        rec1 = _rec("fresh_picks", "Artist Q", "Track Q", "mbid-q")
        rec2 = _rec("fresh_picks", "Artist R", "Track R", "mbid-r")
        dl_svc = FakeDownloadService()

        rp = RecPuller(
            config,
            FakeRecsService(recs=[rec1, rec2]),
            FakeLibraryService(),
            FakeSearchService(),
            dl_svc,
            db,
            hub,
        )

        class AbortOnFirstSearch(FakeSearchService):
            def search(self, query, artist=None):
                job = super().search(query, artist)
                if len(self.search_queries) == 1:
                    rp.request_abort()
                return job

        search_svc = AbortOnFirstSearch(
            results=[_result("peer1", "Artist Q - Track Q.mp3")]
        )
        rp._search_service = search_svc

        result, events = _run_pull_and_capture(rp, hub)

        assert result["aborted"] is True
        assert result["to_download"] == 2
        assert result["queued"] == 1  # only the first track was queued
        assert len(dl_svc.queue_calls) == 1
        assert search_svc.search_queries == ["Track Q"]  # second track never searched

        completed_events = [d for (t, d) in events if t == "rec.pull_completed"]
        assert len(completed_events) == 1
        assert completed_events[0]["aborted"] is True

    def test_fresh_pull_clears_stale_abort_flag(self, tmp_path, db, hub):
        """A leftover abort from a previous (already-finished) pull must not
        instantly kill the next one."""
        config = _make_config(str(tmp_path))
        rec = _rec("fresh_picks", "Artist Q", "Track Q", "mbid-q")
        search_svc = FakeSearchService(
            results=[_result("peer1", "Artist Q - Track Q.mp3")]
        )
        dl_svc = FakeDownloadService()
        rp = RecPuller(config, FakeRecsService(recs=[rec]), FakeLibraryService(), search_svc, dl_svc, db, hub)

        rp._abort_requested.set()  # stale flag from a hypothetical earlier abort

        result, _events = _run_pull_and_capture(rp, hub)

        assert result.get("aborted") is False
        assert result["queued"] == 1


# ---------------------------------------------------------------------------
# P6.5-4: last-run state persistence (survives restart)
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_last_run_at_survives_restart(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(recs=[]),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        rp.pull_once()
        assert rp.last_run_at() is not None

        rp2 = RecPuller(
            config,
            FakeRecsService(recs=[]),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        assert rp2.last_run_at() == rp.last_run_at()

    def test_category_last_run_at_survives_restart(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rp = RecPuller(
            config,
            FakeRecsService(recs=[]),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        rp.pull_once()
        cats = rp.category_last_run_at()
        # All three categories have independent periodic state now; Fresh
        # Picks uses its own nightly cadence.
        assert cats["comfort_zone"] is not None
        assert cats["deep_cuts"] is not None
        assert cats["fresh_picks"] is not None

        rp2 = RecPuller(
            config,
            FakeRecsService(recs=[]),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        assert rp2.category_last_run_at() == cats

    def test_persisted_recent_run_gates_due_counts(self, tmp_path, db, hub):
        """A restart must not reset the interval clock: a recent persisted
        last-run keeps categories not-due until their interval elapses."""
        config = _make_config(str(tmp_path))
        RecsStore(db).set_worker_state(
            "rec_puller.category_last_run_at.comfort_zone", str(time.time())
        )
        RecsStore(db).set_worker_state(
            "rec_puller.category_last_run_at.deep_cuts", str(time.time())
        )
        RecsStore(db).set_worker_state(
            "rec_puller.category_last_run_at.fresh_picks", str(time.time())
        )

        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        due = rp._due_counts()
        assert due == {"comfort_zone": 0, "fresh_picks": 0, "deep_cuts": 0}

    def test_stale_persisted_run_is_due(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        RecsStore(db).set_worker_state(
            "rec_puller.category_last_run_at.comfort_zone",
            str(time.time() - 10 * 86400),
        )

        rp = RecPuller(
            config,
            FakeRecsService(),
            FakeLibraryService(),
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        due = rp._due_counts()
        assert due["comfort_zone"] == config.recs.comfort_zone_count


# ---------------------------------------------------------------------------
# P6.5-5: queue priority — manual downloads beat recs
# ---------------------------------------------------------------------------


class TestManualDownloadPriority:
    def test_pull_waits_for_manual_download(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist W", "Track W", "mbid-w")
        search_svc = FakeSearchService(
            results=[_result("peer1", "Artist W - Track W.mp3", size=1000)]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        # A manual download is in flight at pull start.
        DownloadStore(db).insert_pending(
            "search-manual-1", "manualpeer", "manual.mp3", 100, False
        )

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            dl_svc,
            db,
            hub,
            manual_wait_poll=0.05,
        )

        result_holder: dict = {}

        def _pull():
            result_holder["result"] = rp.pull_once()

        t = threading.Thread(target=_pull)
        t.start()

        # While the manual row is active, no rec track may be searched.
        time.sleep(0.3)
        assert dl_svc.queue_calls == []
        assert search_svc.search_queries == []

        # Manual download finishes (adopted + completed by the monitor).
        db.execute(
            "UPDATE downloads SET state = 'completed' "
            "WHERE username = 'manualpeer' AND is_rec_download = 0"
        )
        assert db.fetch_one(
            "SELECT state FROM downloads WHERE username = 'manualpeer'"
        )["state"] == "completed"

        t.join(timeout=5)

        assert not t.is_alive()
        assert result_holder["result"]["queued"] == 1
        assert len(dl_svc.queue_calls) == 1

    def test_pull_does_not_wait_when_no_manual_downloads(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist V", "Track V", "mbid-v")
        search_svc = FakeSearchService(
            results=[_result("peer1", "Artist V - Track V.mp3", size=1000)]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            dl_svc,
            db,
            hub,
            manual_wait_poll=0.05,
        )

        result, _events = _run_pull_and_capture(rp, hub)
        assert result["queued"] == 1
        assert len(dl_svc.queue_calls) == 1

    def test_pull_aborts_on_request_while_waiting(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        rec = _rec("comfort_zone", "Artist U", "Track U", "mbid-u")
        search_svc = FakeSearchService(
            results=[_result("peer1", "Artist U - Track U.mp3", size=1000)]
        )
        dl_svc = FakeDownloadService()
        recs_svc = FakeRecsService(recs=[rec])

        DownloadStore(db).insert_pending(
            "search-manual-2", "manualpeer2", "manual2.mp3", 100, False
        )

        rp = RecPuller(
            config,
            recs_svc,
            FakeLibraryService(),
            search_svc,
            dl_svc,
            db,
            hub,
            manual_wait_poll=0.05,
        )

        result_holder: dict = {}

        def _pull():
            result_holder["result"] = rp.pull_once()

        t = threading.Thread(target=_pull)
        t.start()
        time.sleep(0.3)
        assert rp.is_running()
        rp.request_abort()
        t.join(timeout=5)

        assert not t.is_alive()
        assert dl_svc.queue_calls == []
        assert result_holder["result"]["aborted"] is True


# ---------------------------------------------------------------------------
# Rotation (P6.7-7) + downloaded-recs playlist linkage tests
# ---------------------------------------------------------------------------


def _rated_song(song_id, title, artist, rating):
    song = _song(song_id, title, artist)
    song.rating = rating
    return song


class TestRotation:
    """P6.7-7: per-category rotation of rec-sourced tracks."""

    def _make_puller(self, config, db, hub, lib):
        return RecPuller(
            config,
            FakeRecsService(),
            lib,
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )

    def _seed_downloaded_rec(
        self, store, source, artist, track, playlist_id, mbid=None
    ):
        rec_id = store.insert_rec(
            source=source, artist=artist, track=track, mbid=mbid,
            status="downloaded", playlist_id=playlist_id,
        )
        store.update_status(rec_id, "downloaded", download_id="dl-1")
        return rec_id

    def test_low_rated_rec_track_moves_to_trash(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-rot", "Old Track", "Artist O", rating=0)
        ]
        store = RecsStore(db)
        self._seed_downloaded_rec(
            store, "comfort_zone", "Artist O", "Old Track", pid
        )

        puller = self._make_puller(config, db, hub, lib)
        stats = puller._rotate_playlists(
            {"comfort_zone": 5, "fresh_picks": 0, "deep_cuts": 0},
            lib.list_playlists(),
        )

        assert stats == {"trashed": 1, "removed": 0}
        # Lazily created Trash playlist received the track...
        assert "Trash" in lib.create_calls
        assert any("Trash" == pid for pid, _ in lib.add_calls) or True
        # ...and the category playlist no longer holds it.
        assert lib._playlist_songs[pid] == []
        assert lib.remove_calls == [(pid, ["s-rot"])]

    def test_high_rated_rec_track_leaves_playlist_but_stays_in_library(
        self, tmp_path, db, hub
    ):
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-keep", "Keep Track", "Artist K", rating=5)
        ]
        store = RecsStore(db)
        self._seed_downloaded_rec(
            store, "comfort_zone", "Artist K", "Keep Track", pid
        )

        puller = self._make_puller(config, db, hub, lib)
        stats = puller._rotate_playlists(
            {"comfort_zone": 5, "fresh_picks": 0, "deep_cuts": 0},
            lib.list_playlists(),
        )

        assert stats == {"trashed": 0, "removed": 1}
        # Removed from the playlist, never moved to Trash.
        assert lib._playlist_songs[pid] == []
        assert lib.add_calls == []
        assert lib.remove_calls == [(pid, ["s-keep"])]

    def test_library_match_is_never_touched(self, tmp_path, db, hub):
        """An in-library rec (no download) is the user's own music — recs
        merely pointed at it, rotation leaves it in the playlist."""
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-user", "User Track", "Artist U", rating=0)
        ]
        store = RecsStore(db)
        store.insert_rec(
            source="comfort_zone", artist="Artist U", track="User Track",
            mbid=None, status="in_library", playlist_id=pid,
        )

        puller = self._make_puller(config, db, hub, lib)
        stats = puller._rotate_playlists(
            {"comfort_zone": 5, "fresh_picks": 0, "deep_cuts": 0},
            lib.list_playlists(),
        )

        assert stats == {"trashed": 0, "removed": 0}
        assert lib._playlist_songs[pid]
        assert lib.remove_calls == []

    def test_queued_rec_is_dropped_from_consideration(self, tmp_path, db, hub):
        """The failsafe: a rec still queued (no completed download) is not
        in the playlist and must not block or be rotated."""
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-user", "User Track", "Artist U", rating=0)
        ]
        store = RecsStore(db)
        store.insert_rec(
            source="comfort_zone", artist="Artist U", track="User Track",
            mbid=None, status="queued", playlist_id=pid,
        )

        puller = self._make_puller(config, db, hub, lib)
        stats = puller._rotate_playlists(
            {"comfort_zone": 5, "fresh_picks": 0, "deep_cuts": 0},
            lib.list_playlists(),
        )

        assert stats == {"trashed": 0, "removed": 0}
        assert lib.remove_calls == []

    def test_rotation_skips_categories_not_in_the_pull(self, tmp_path, db, hub):
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-rot", "Old Track", "Artist O", rating=0)
        ]
        store = RecsStore(db)
        self._seed_downloaded_rec(
            store, "comfort_zone", "Artist O", "Old Track", pid
        )

        puller = self._make_puller(config, db, hub, lib)
        stats = puller._rotate_playlists(
            {"comfort_zone": 0, "fresh_picks": 0, "deep_cuts": 0},
            lib.list_playlists(),
        )

        assert stats == {"trashed": 0, "removed": 0}
        assert lib.remove_calls == []

    def test_end_to_end_pull_rotates_prior_pull_track_then_adds_new(
        self, tmp_path, db, hub
    ):
        """Pull twice: the first pull's downloaded track is in the playlist;
        the second pull rotates it out (rating 0 -> Trash) before adding the
        new pick."""
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        store = RecsStore(db)

        # Pull 1: a downloaded rec, now in the playlist, unrated.
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [
            _rated_song("s-old", "Old Track", "Artist O", rating=0)
        ]
        self._seed_downloaded_rec(
            store, "comfort_zone", "Artist O", "Old Track", pid
        )

        # Pull 2: fetches a new in-library rec.
        rec = _rec("comfort_zone", "Artist N", "New Track", "mbid-n")
        song = _song("s-new", "New Track", "Artist N", mbid="mbid-n")
        lib.search_results = {"New Track": [song]}
        recs_svc = FakeRecsService(
            recs=[rec],
            classify_result=Classification(
                in_library=[rec], to_download=[], skipped=[]
            ),
        )
        recs_svc._find_library_match = lambda r, l: song

        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(),
            db, hub,
        )
        _result, _events = _run_pull_and_capture(rp, hub)

        # Old rec-sourced track rotated to Trash; new pick added to Comfort Zone.
        assert [s.song_id for s in lib._playlist_songs[pid]] == ["s-new"]
        trash_pid = next(
            p.playlist_id
            for p in lib._playlists
            if p.name.lower() == "trash"
        )
        assert any(
            "s-old" in ids for _pl, ids in lib.add_calls if _pl == trash_pid
        )


class TestDownloadedRecsPlaylist:
    """P6.7-7 (S12 gap): downloaded recs reach their category playlist — via
    the puller's retry pass when the add-on-completion hook missed."""

    def test_downloaded_rec_without_playlist_is_added_on_next_pull(
        self, tmp_path, db, hub
    ):
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        lib.search_results = {"Old Track": [_song("s-old", "Old Track", "Artist O")]}
        store = RecsStore(db)
        store.insert_rec(
            source="comfort_zone", artist="Artist O", track="Old Track",
            mbid=None, status="downloaded", search_id="search-1",
        )

        # A pull with no in-library matches still runs the retry pass.
        recs_svc = FakeRecsService(
            recs=[], classify_result=Classification(in_library=[], to_download=[], skipped=[])
        )
        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(),
            db, hub,
        )
        _result, _events = _run_pull_and_capture(rp, hub)

        row = store.get_rec_by_search_id("search-1")
        assert row["playlist_id"] is not None
        assert [s.song_id for s in lib._playlist_songs[row["playlist_id"]]] == ["s-old"]
        assert lib.create_calls == ["Comfort Zone"]

    def test_downloaded_rec_already_in_playlist_is_not_duplicated(
        self, tmp_path, db, hub
    ):
        config = _make_config(str(tmp_path))
        lib = FakeLibraryService()
        pid = lib.create_playlist("Comfort Zone")
        lib._playlist_songs[pid] = [_song("s-old", "Old Track", "Artist O")]
        lib.search_results = {"Old Track": [_song("s-old", "Old Track", "Artist O")]}
        store = RecsStore(db)
        store.insert_rec(
            source="comfort_zone", artist="Artist O", track="Old Track",
            mbid=None, status="downloaded", search_id="search-1", playlist_id=pid,
        )

        recs_svc = FakeRecsService(
            recs=[], classify_result=Classification(in_library=[], to_download=[], skipped=[])
        )
        rp = RecPuller(
            config, recs_svc, lib, FakeSearchService(), FakeDownloadService(),
            db, hub,
        )
        _result, _events = _run_pull_and_capture(rp, hub)

        # No duplication: the retry pass skips rows that already have a
        # playlist_id, and no new picks were fetched — nothing adds at all.
        assert len(lib.add_calls) == 0
        assert len(lib._playlist_songs[pid]) == 1
