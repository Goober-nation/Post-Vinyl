"""
Tests for #3: library-wide rating sweep (TrashPurge._sweep_library_ratings).

Before this, the only path that ever added a low-rated track to Trash was
RecPuller trimming Fresh Picks on overflow (rec_puller.py) — a track rated
<=1 in a manual playlist, a different rec playlist, or no playlist at all
never reached Trash. See TrashPurge._sweep_library_ratings.
"""

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.playlist_store import PlaylistStore
from app.db.sync_store import SyncStore
from app.services.library import PlaylistDetail, PlaylistInfo, Song
from app.workers.trash_purge import TRASH_PLAYLIST_NAME, TrashPurge

import pytest


def _make_config(tmpdir, rotation_trash_rating=1):
    from pathlib import Path

    p = Path(tmpdir)

    class MockPaths:
        pass

    paths = MockPaths()
    paths.data_dir = str(p / "data")
    paths.music_dir = p / "music"
    paths.download_dir = "downloads"
    paths.download_path = p / "music" / "downloads"

    class MockSync:
        interval_hours = 12
        love_enabled = True
        hate_enabled = False
        star_rating_enabled = True
        trash_deletion_enabled = False

    class MockRecs:
        pass

    MockRecs.rotation_trash_rating = rotation_trash_rating

    class MockConfig:
        pass

    cfg = MockConfig()
    cfg.paths = paths
    cfg.sync = MockSync()
    cfg.recs = MockRecs()
    return cfg


def _song(song_id, rating=0, title="Track"):
    return Song(
        song_id=song_id,
        title=title,
        artist="Artist",
        album="Album",
        path=f"{song_id}.flac",
        duration=200,
        size=1000,
        bitrate=320,
        track_number=1,
        year=2020,
        genre="Rock",
        rating=rating,
        starred=False,
        mbid=None,
    )


class FakeLibraryService:
    """Minimal fake covering what the rating sweep + Trash processing need."""

    def __init__(self, low_rated=None, playlists=None):
        self.low_rated = list(low_rated or [])
        self.playlists: dict[str, list[Song]] = dict(playlists or {})
        self.playlist_names: dict[str, str] = {
            pid: pid for pid in self.playlists
        }
        self._next_id = 1
        self.real_paths: dict[str, str | None] = {}
        self.scan_count = 0
        self.removed: list[tuple[str, str]] = []
        self.add_calls: list[tuple[str, list[str]]] = []

    def get_low_rated_songs(self, max_rating):
        return [s for s in self.low_rated if (s.rating or 0) <= max_rating]

    def list_playlists(self):
        return [
            PlaylistInfo(pid, self.playlist_names[pid], len(songs))
            for pid, songs in self.playlists.items()
        ]

    def create_playlist(self, name):
        pid = f"pl-{self._next_id}"
        self._next_id += 1
        self.playlists[pid] = []
        self.playlist_names[pid] = name
        return pid

    def get_playlist_detail(self, playlist_id):
        songs = self.playlists.get(playlist_id, [])
        return PlaylistDetail(playlist_id, self.playlist_names.get(playlist_id, ""), list(songs))

    def add_to_playlist(self, playlist_id, song_ids):
        self.add_calls.append((playlist_id, list(song_ids)))
        current = {s.song_id for s in self.playlists.setdefault(playlist_id, [])}
        for sid in song_ids:
            if sid not in current:
                match = next((s for s in self.low_rated if s.song_id == sid), None)
                self.playlists[playlist_id].append(match or _song(sid))
        return True

    def remove_songs_from_playlist(self, playlist_id, song_ids):
        self.removed.extend((playlist_id, sid) for sid in song_ids)
        self.playlists[playlist_id] = [
            s for s in self.playlists.get(playlist_id, []) if s.song_id not in song_ids
        ]
        return True

    def get_song_real_path(self, song_id):
        return self.real_paths.get(song_id)

    def trigger_scan(self):
        self.scan_count += 1
        return True


class FakeFeedbackService:
    def send_feedback(self, mbid, score):
        return True


@pytest.fixture
def db(tmp_path):
    config = _make_config(str(tmp_path))
    database = Database(config)
    database.initialize_schema()
    yield database
    database.close()


def _make_worker(config, db, library, feedback=None):
    return TrashPurge(
        config,
        library,
        feedback or FakeFeedbackService(),
        db,
        sync_store=SyncStore(db),
        download_store=DownloadStore(db),
        playlist_store=PlaylistStore(db),
    )


class TestLibraryRatingSweep:
    def test_library_track_with_no_playlist_reaches_trash(self, db, tmp_path):
        """T3a (fixed): a rated-<=1 track with no playlist membership at
        all is now swept into Trash — the core defect."""
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(low_rated=[_song("s1", rating=1)])
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 1
        trash_id = next(iter(library.playlists))
        assert {s.song_id for s in library.playlists[trash_id]} == {"s1"}

    def test_manual_and_rec_playlist_tracks_also_reach_trash(self, db, tmp_path):
        """T3c: rated-<=1 tracks across a manual playlist, a rec playlist,
        and no playlist at all all converge on Trash in one cycle,
        regardless of playlist membership."""
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(
            low_rated=[
                _song("manual-1", rating=0),
                _song("rec-1", rating=1),
                _song("none-1", rating=1),
            ],
            playlists={
                "manual-pl": [_song("manual-1", rating=0)],
                "rec-pl": [_song("rec-1", rating=1)],
            },
        )
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 3
        trash_id = next(pid for pid in library.playlists if pid not in ("manual-pl", "rec-pl"))
        assert {s.song_id for s in library.playlists[trash_id]} == {
            "manual-1",
            "rec-1",
            "none-1",
        }

    def test_already_trashed_song_is_not_re_added(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(
            low_rated=[_song("s1", rating=1)],
            playlists={"trash-pl": [_song("s1", rating=1)]},
        )
        library.playlist_names["trash-pl"] = TRASH_PLAYLIST_NAME
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 0
        assert library.add_calls == []

    def test_no_low_rated_songs_is_a_noop(self, db, tmp_path):
        config = _make_config(str(tmp_path))
        library = FakeLibraryService(low_rated=[])
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 0
        assert library.playlists == {}

    def test_rotation_trash_rating_threshold_is_respected(self, db, tmp_path):
        """A song rated above the configured threshold is left alone."""
        config = _make_config(str(tmp_path), rotation_trash_rating=1)
        library = FakeLibraryService(
            low_rated=[_song("low", rating=1), _song("high", rating=3)]
        )
        library.get_low_rated_songs = lambda max_rating: [
            s for s in [_song("low", rating=1), _song("high", rating=3)]
            if (s.rating or 0) <= max_rating
        ]
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 1
        trash_id = next(iter(library.playlists))
        assert {s.song_id for s in library.playlists[trash_id]} == {"low"}

    def test_swept_song_is_purged_in_the_same_cycle_when_deletion_enabled(
        self, db, tmp_path
    ):
        """The sweep runs before the Trash-processing step, so a
        newly-swept song is handled in the same purge_once() call rather
        than waiting a full cycle."""
        config = _make_config(str(tmp_path))
        config.sync.trash_deletion_enabled = True
        real = config.paths.music_dir / "s1.flac"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("data")
        library = FakeLibraryService(low_rated=[_song("s1", rating=0)])
        library.real_paths["s1"] = "s1.flac"
        worker = _make_worker(config, db, library)

        result = worker.purge_once()

        assert result["swept"] == 1
        assert result["trashed"] == 1
        assert not real.exists()
