"""
Issue #3: "trash pass does not check all of the library" — 1-star rated
tracks are only ever purged if they happen to be sitting in a rec category
playlist (and only when Fresh Picks' rolling cap forces an eviction). A
track rated 1-star anywhere else in the library — a manual playlist, or no
playlist at all — is never detected.

Root cause: the only code path that ever adds a rated-<=threshold track to
the Trash playlist is RecPuller._write_category_playlist's Fresh Picks
overflow trim (app/workers/rec_puller.py). TrashPurge itself correctly
processes whatever is already in the Trash playlist; nothing ever puts
non-rec tracks there.

Test plan posted to https://github.com/Goober-nation/Post-Vinyl/issues/3
"""

from pathlib import Path

import pytest

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.sync_store import SyncStore
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.sse import EventHub
from app.workers.rec_puller import RecPuller
from app.workers.trash_purge import TRASH_PLAYLIST_NAME, TrashPurge


def _make_config(tmpdir):
    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.music_dir = p / "music"
    paths.download_dir = "downloads"
    paths.download_path = p / "music" / "downloads"
    paths.searches_dir = str(p / "searches")
    paths.discovery_dir = str(p / "discovery")

    class MockDownload:
        check_interval = 15
        max_retries_per_track = 3
        bad_peer_threshold = 3

    class MockRecs:
        pass

    MockRecs.comfort_zone_enabled = False
    MockRecs.fresh_picks_enabled = False
    MockRecs.deep_cuts_enabled = False
    MockRecs.comfort_zone_interval_days = 1
    MockRecs.deep_cuts_interval_days = 7
    MockRecs.comfort_zone_playlist_name = "Comfort Zone"
    MockRecs.fresh_picks_playlist_name = "Fresh Picks"
    MockRecs.deep_cuts_playlist_name = "Deep Cuts"
    MockRecs.comfort_zone_count = 0
    MockRecs.deep_cuts_count = 0
    MockRecs.rotation_trash_rating = 1

    class MockFreshPicks:
        count = 0
        offset = 0
        pull_window = "30d"
        search_buffer = 0

    class MockLB:
        enabled = True

    class MockSync:
        interval_hours = 12
        love_enabled = True
        hate_enabled = True
        star_rating_enabled = True
        trash_deletion_enabled = True

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.download = MockDownload()
    cfg.recs = MockRecs()
    cfg.fresh_picks = MockFreshPicks()
    cfg.listenbrainz = MockLB()
    cfg.sync = MockSync()
    return cfg


def _song(song_id, title, mbid=None, rating=0, path="some/path/file.flac"):
    return Song(
        song_id=song_id,
        title=title,
        artist="Artist",
        album="Album",
        path=path,
        duration=200,
        size=1000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=rating,
        starred=False,
        mbid=mbid,
    )


class FakeLibraryService:
    """Combined fake covering both RecPuller's and TrashPurge's calls, so a
    single instance can stand in for "the whole library" across one
    end-to-end sync+purge cycle."""

    def __init__(self):
        self._playlists: list[PlaylistInfo] = []
        self._playlist_songs: dict[str, list[Song]] = {}
        self._next_playlist_id = 1
        self.real_paths: dict[str, str | None] = {}
        self.scan_count = 0
        self.create_calls: list[str] = []
        self.add_calls: list[tuple] = []
        self.remove_calls: list[tuple] = []

    # -- shared / RecPuller side --
    def search_library(self, query):
        return []

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
            if not any(s.song_id == song_id for s in songs):
                songs.append(self.all_songs.get(song_id) or _song(song_id, song_id))
        return True

    def get_playlist_detail(self, playlist_id):
        return PlaylistDetail(
            playlist_id=playlist_id,
            name="",
            songs=list(self._playlist_songs.get(playlist_id, [])),
        )

    def remove_songs_from_playlist(self, playlist_id, song_ids):
        self.remove_calls.append((playlist_id, list(song_ids)))
        self._playlist_songs[playlist_id] = [
            s for s in self._playlist_songs.get(playlist_id, []) if s.song_id not in song_ids
        ]
        return True

    def trigger_scan(self):
        self.scan_count += 1
        return True

    # -- TrashPurge side --
    def get_song_real_path(self, song_id):
        return self.real_paths.get(song_id)

    # -- test setup helper --
    def seed_playlist(self, name, songs):
        pid = f"pl-{self._next_playlist_id}"
        self._next_playlist_id += 1
        self._playlists.append(PlaylistInfo(pid, name, len(songs)))
        self._playlist_songs[pid] = list(songs)
        return pid

    @property
    def all_songs(self) -> dict[str, Song]:
        out: dict[str, Song] = {}
        for songs in self._playlist_songs.values():
            for s in songs:
                out[s.song_id] = s
        return out


class FakeFeedbackService:
    def __init__(self):
        self.sent: list[tuple[str, int]] = []

    def send_feedback(self, mbid, score):
        self.sent.append((mbid, score))
        return True


class FakeRecsService:
    def fetch_recommendations(self, counts):
        return []

    def classify(self, recs, library):
        from app.services.interfaces.recommendation import Classification

        return Classification(in_library=[], to_download=[], skipped=[])


class FakeSearchService:
    def search(self, query, artist=None):
        raise NotImplementedError

    def get_results(self, search_id):
        return []


class FakeDownloadService:
    def queue(self, username, files, search_id=None, destination=None):
        raise NotImplementedError

    def get_status(self):
        return []


@pytest.fixture
def db(tmp_path):
    config = _make_config(str(tmp_path))
    database = Database(config)
    database.initialize_schema()
    yield database
    database.close()


@pytest.fixture
def hub():
    return EventHub()


def _sync_and_purge_cycle(config, db, hub, library):
    """One full "sync & purge pass" as a user would trigger it: a rec pull
    (which is the only place a rating->Trash mapping exists today, via Fresh
    Picks' overflow trim) followed by TrashPurge's own sweep."""
    rp = RecPuller(
        config,
        FakeRecsService(),
        library,
        FakeSearchService(),
        FakeDownloadService(),
        db,
        hub,
    )
    rp.pull_once()
    tp = TrashPurge(
        config,
        library,
        FakeFeedbackService(),
        db,
        sync_store=SyncStore(db),
        download_store=DownloadStore(db),
    )
    return tp.purge_once()


class TestLibraryWideRatingGap:
    def test_one_starred_track_outside_any_rec_playlist_is_never_trashed(
        self, db, tmp_path, hub
    ):
        """T3a — the core defect. A track rated 1-star in a manual playlist
        (not a rec category, no overflow event to trigger anything) must, per
        the issue, end up purged on the next sync & purge pass. It doesn't."""
        config = _make_config(str(tmp_path))
        library = FakeLibraryService()
        library.seed_playlist(
            "My Favorites", [_song("s1", "Track 1", "mbid-1", rating=1)]
        )

        _sync_and_purge_cycle(config, db, hub, library)

        # Passes today: proves the gap. The song was never moved to Trash
        # and never removed from its playlist.
        assert not any(
            call[0] != "My Favorites" and "s1" in call[1] for call in library.remove_calls
        )
        assert all("s1" not in songs for _pid, songs in library.add_calls)

    def test_one_starred_track_with_no_playlist_at_all_is_never_trashed(
        self, db, tmp_path, hub
    ):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService()
        # Not attached to any playlist — just present in the library.
        library._playlist_songs.setdefault("__none__", [])
        stray = _song("s2", "Stray track", "mbid-2", rating=1)
        library.real_paths["s2"] = "/music/stray.mp3"
        # No seed_playlist call: nothing references s2 via any playlist API,
        # matching "no playlist at all".

        _sync_and_purge_cycle(config, db, hub, library)

        assert library.add_calls == []
        assert library.remove_calls == []


class TestFreshPicksPartialCoverage:
    def test_low_rated_track_stays_when_no_overflow_forces_a_trim(
        self, db, tmp_path, hub
    ):
        """T3b — Fresh Picks only evicts on overflow, not proactively. A
        1-star track already in Fresh Picks, with capacity to spare, is
        never rotated out just because it's rated low."""
        config = _make_config(str(tmp_path), )
        config.fresh_picks.count = 5  # plenty of headroom
        library = FakeLibraryService()
        playlist_id = library.seed_playlist(
            "Fresh Picks", [_song("old-low", "Old low", "Artist", rating=1)]
        )

        rp = RecPuller(
            config,
            FakeRecsService(),
            library,
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        # No new songs at all — nothing to force an overflow trim.
        assert rp._write_category_playlist(
            "fresh_picks", playlist_id, [], library._playlists
        )

        remaining = [s.song_id for s in library._playlist_songs[playlist_id]]
        assert "old-low" in remaining  # still there — rating alone never evicted it
        assert library.remove_calls == []

    def test_overflow_only_drops_as_many_as_the_overflow_count(self, db, tmp_path, hub):
        """Companion case: 3 tracks are 1-star-eligible, but overflow is 1 —
        only 1 is dropped, the other 2 stay rated-low in Fresh Picks
        indefinitely until a future overflow happens to reach them."""
        config = _make_config(str(tmp_path))
        config.fresh_picks.count = 3
        library = FakeLibraryService()
        existing = [
            _song("low-1", "Low 1", "Artist", rating=1),
            _song("low-2", "Low 2", "Artist", rating=1),
            _song("low-3", "Low 3", "Artist", rating=1),
        ]
        playlist_id = library.seed_playlist("Fresh Picks", existing)

        rp = RecPuller(
            config,
            FakeRecsService(),
            library,
            FakeSearchService(),
            FakeDownloadService(),
            db,
            hub,
        )
        # One new song added -> overflow = (3 existing + 1 new) - target(3) = 1
        assert rp._write_category_playlist(
            "fresh_picks", playlist_id, ["new-song"], library._playlists
        )

        remaining = [s.song_id for s in library._playlist_songs[playlist_id]]
        assert len(remaining) == 3
        dropped = [sid for sid in ("low-1", "low-2", "low-3") if sid not in remaining]
        assert len(dropped) == 1  # only the overflow amount, not all 3 eligible


class TestDesiredLibraryWideSweep:
    @pytest.mark.xfail(
        reason="no library-wide rating sweep exists yet — issue #3", strict=True
    )
    def test_all_low_rated_tracks_anywhere_are_purged_by_one_sync_cycle(
        self, db, tmp_path, hub
    ):
        """T3c — spec for the fix. Regardless of where a <=1-star track
        lives (a rec playlist, a manual playlist, or no playlist at all),
        one sync & purge pass should find it and purge it."""
        config = _make_config(str(tmp_path))
        library = FakeLibraryService()
        library.seed_playlist(
            "Comfort Zone", [_song("in-rec", "In rec", "mbid-a", rating=1)]
        )
        library.seed_playlist(
            "My Favorites", [_song("in-manual", "In manual", "mbid-b", rating=1)]
        )
        library.real_paths["stray"] = "/music/stray.mp3"

        _sync_and_purge_cycle(config, db, hub, library)

        trashed = {sid for _pid, sids in library.add_calls for sid in sids}
        assert {"in-rec", "in-manual", "stray"} <= trashed
