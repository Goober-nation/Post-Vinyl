"""
Tests for BeetsService (P6.6-1).

The subprocess call is mocked throughout — the real `beet import` is
exercised only by the integration check against the built image. What is
tested here is everything around it: which command gets built, how the
per-profile config routes discovery vs. searches, how the imported item is
located afterward, and that every failure mode returns "not moved" rather
than raising or reporting a bogus path.
"""

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.beets import BeetsService


def _make_config(tmp_path, binary="beet", timeout=120):
    class Paths:
        data_dir = str(tmp_path / "data")
        searches_dir = str(tmp_path / "searches")
        discovery_dir = str(tmp_path / "discovery")
        # BeetsService resolves via the *_path properties in real
        # PathsConfig (music_dir / relative suffix) — mirror that here.
        searches_path = tmp_path / "searches"
        discovery_path = tmp_path / "discovery"
        discovery_familiar_path = tmp_path / "discovery" / "Comfort_Zone"
        discovery_new_releases_path = tmp_path / "discovery" / "New_Releases"
        discovery_exploration_path = tmp_path / "discovery" / "Deep_Cuts"
        library_path = tmp_path / "library"

    class Beets:
        pass

    Beets.binary = binary
    Beets.timeout_seconds = timeout
    Beets.enabled = True

    class Config:
        pass

    cfg = Config()
    cfg.paths = Paths()
    cfg.beets = Beets()
    return cfg


def _seed_library(db_path: Path, path: str, added: float, mb_trackid=None):
    """Write the minimal beets library schema BeetsService reads back.

    `id` and `albums` are here because they are in the real schema and
    BeetsService uses them: it prunes item rows by id and drops album rows
    left with no items.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items "
            "(id INTEGER PRIMARY KEY, path BLOB, added REAL, "
            "mb_trackid TEXT, mb_albumid TEXT, album_id INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS albums (id INTEGER PRIMARY KEY, album TEXT)"
        )
        conn.execute(
            "INSERT INTO items (path, added, mb_trackid, mb_albumid) "
            "VALUES (?, ?, ?, NULL)",
            (path.encode(), added, mb_trackid),
        )


def _item_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


@pytest.fixture
def config(tmp_path):
    return _make_config(tmp_path)


class TestCommandConstruction:
    def test_invokes_configured_binary_with_profile_config(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False)

        argv = run.call_args.args[0]
        assert argv[0] == "beet"
        assert argv[1:3] == ["--config", str(tmp_path / "data/beets/searches.yaml")]
        assert argv[3:6] == ["import", "-q", "-s"]  # -s: singleton match
        assert argv[6] == str(source)
        assert run.call_args.kwargs["timeout"] == 120

    def test_rec_and_manual_use_separate_profiles(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=True, category="comfort_zone")
            service.import_file(source, is_rec=True, category="fresh_picks")
            service.import_file(source, is_rec=True, category="deep_cuts")
            service.import_file(source, is_rec=False)

        configs = [call.args[0][2] for call in run.call_args_list]
        assert configs[0].endswith("discovery_familiar.yaml")
        assert configs[1].endswith("discovery_new_releases.yaml")
        assert configs[2].endswith("discovery_exploration.yaml")
        assert configs[3].endswith("searches.yaml")

    def test_unresolvable_category_fails_without_running_beets(
        self, config, tmp_path
    ):
        """A rec whose category can't be resolved has no destination (the
        old 'discovery' fallback profile was dropped) — the import must
        fail before beets ever runs, leaving the source file untouched."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            for bad in (None, "", "weekly_jams", "musicbrainz"):
                result = service.import_file(source, is_rec=True, category=bad)
                assert result.ok is False
                assert result.target_path is None
                assert "no resolvable category" in result.error
        run.assert_not_called()
        assert source.exists()

    def test_profile_config_points_at_the_right_tree(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=True, category="comfort_zone")

        written = (
            tmp_path / "data/beets/discovery_familiar.yaml"
        ).read_text()
        assert f"directory: {tmp_path / 'discovery' / 'Comfort_Zone'}" in written
        # quiet_fallback: asis is what makes P6.6-4 work — an unmatched file
        # is imported with its own tags rather than skipped.
        assert "quiet_fallback: asis" in written
        assert "move: yes" in written

    def test_target_tree_is_created(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=True, category="deep_cuts")

        assert (tmp_path / "discovery" / "Deep_Cuts").is_dir()


def _land(tmp_path, relative: str) -> Path:
    """Create the file beets would have moved into place."""
    return _land_in(tmp_path, "searches", relative)


def _land_in(tmp_path, profile: str, relative: str) -> Path:
    """Create a file inside a given profile's tree."""
    path = tmp_path / profile / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("data")
    return path


class FakeMusicBrainz:
    """Stands in for MusicBrainzClient — returns a canned recording (or
    None) instead of calling the real API, and records what it was asked."""

    def __init__(self, recording=None):
        self.recording = recording
        self.calls: list[tuple[str, str, int]] = []
        self.lookup_calls: list[str] = []

    def resolve_canonical(self, title, artist, min_score=90):
        self.calls.append((title, artist, min_score))
        return self.recording

    def lookup_recording(self, mbid):
        self.lookup_calls.append(mbid)
        return self.recording

    def lookup_release_tracks(self, release_mbid):
        return None


class TestMusicBrainzConstraint:
    """P-MB-1 wiring: import_file() must ask MusicBrainz what the user
    actually asked for and pin the beets match to it, rather than trusting
    whatever identity the downloaded file's own tags claim."""

    def test_resolved_recording_pins_search_id_and_forces_from_scratch(
        self, config, tmp_path
    ):
        from app.services.interfaces.musicbrainz import MBRecording

        recording = MBRecording(
            mbid="abc-123", title="Jóga", artist_credit="Björk", artist="Björk"
        )
        mb = FakeMusicBrainz(recording=recording)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, title="Jóga", artist="Björk")

        argv = run.call_args.args[0]
        assert argv[argv.index("--search-id") + 1] == "abc-123"
        assert "--from-scratch" in argv
        assert mb.calls == [("Jóga", "Björk", 90)]

    def test_resolved_recording_forces_albumartist_and_album_via_set(
        self, config, tmp_path
    ):
        """Singleton mode (`-s`) matches beets against the MusicBrainz
        *recording*, not a release, so beets' own TrackInfo never carries
        albumartist/album — confirmed against beets 2.13.1's
        autotag/hooks.py (only AlbumInfo maps artist -> albumartist).
        import_file must force those fields itself via `--set` rather than
        trusting beets to fill them in, or a resolved recording still lands
        with no albumartist/album (live-verified 2026-08-12 on Björk -
        Jóga)."""
        from app.services.interfaces.musicbrainz import MBRecording, MBRelease

        recording = MBRecording(
            mbid="abc-123",
            title="Jóga",
            artist_credit="Björk",
            artist="Björk",
            releases=[
                MBRelease(mbid="rel-1", title="Homogenic", primary_type="Album")
            ],
        )
        mb = FakeMusicBrainz(recording=recording)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, title="Jóga", artist="Björk")

        argv = run.call_args.args[0]
        set_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]
        assert "albumartist=Björk" in set_pairs
        assert "album=Homogenic" in set_pairs

    def test_resolved_recording_without_release_only_forces_albumartist(
        self, config, tmp_path
    ):
        from app.services.interfaces.musicbrainz import MBRecording

        recording = MBRecording(
            mbid="abc-123", title="Some Song", artist_credit="Nobody", artist="Nobody"
        )
        mb = FakeMusicBrainz(recording=recording)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(
                source, is_rec=False, title="Some Song", artist="Nobody"
            )

        argv = run.call_args.args[0]
        set_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]
        assert set_pairs == ["albumartist=Nobody"]

    def test_no_confident_match_falls_back_to_unconstrained_import(
        self, config, tmp_path
    ):
        mb = FakeMusicBrainz(recording=None)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(
                source, is_rec=False, title="Unknown Track", artist="Unknown Artist"
            )

        argv = run.call_args.args[0]
        assert "--search-id" not in argv
        assert "--from-scratch" not in argv

    def test_missing_title_or_artist_skips_resolution_entirely(self, config, tmp_path):
        mb = FakeMusicBrainz(recording=None)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, title="Jóga", artist=None)
            service.import_file(source, is_rec=False)

        assert mb.calls == []


class TestLibraryDownloads:
    """P6.8: MusicBrainz-initiated downloads route into the "library"
    profile, pinned to an exact recording MBID."""

    def test_library_true_routes_to_the_library_profile(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, library=True)

        argv = run.call_args.args[0]
        assert argv[1:3] == ["--config", str(tmp_path / "data/beets/library.yaml")]

    def test_library_overrides_is_rec_and_category(self, config, tmp_path):
        """library=True wins regardless of is_rec/category — a library
        download is never a rec, and must not fall into a discovery tree."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=True, category="deep_cuts", library=True)

        argv = run.call_args.args[0]
        assert argv[1:3] == ["--config", str(tmp_path / "data/beets/library.yaml")]

    def test_mbid_pins_search_id_without_resolving_canonical(self, config, tmp_path):
        from app.services.interfaces.musicbrainz import MBRecording

        recording = MBRecording(
            mbid="abc-123", title="Jóga", artist_credit="Björk", artist="Björk"
        )
        mb = FakeMusicBrainz(recording=recording)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, library=True, mbid="abc-123")

        argv = run.call_args.args[0]
        assert argv[argv.index("--search-id") + 1] == "abc-123"
        assert "--from-scratch" in argv
        assert mb.lookup_calls == ["abc-123"]
        assert mb.calls == [], "resolve_canonical must not run when an mbid is given"

    def test_mbid_recording_forces_album_fields_via_set(self, config, tmp_path):
        """A successful lookup pins --search-id *and* supplies albumartist/
        album via --set, exactly like the resolve_canonical path."""
        from app.services.interfaces.musicbrainz import MBRecording, MBRelease

        recording = MBRecording(
            mbid="abc-123",
            title="Jóga",
            artist_credit="Björk",
            artist="Björk",
            releases=[MBRelease(mbid="rel-1", title="Homogenic", primary_type="Album")],
        )
        mb = FakeMusicBrainz(recording=recording)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, library=True, mbid="abc-123")

        argv = run.call_args.args[0]
        set_pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--set"]
        assert "albumartist=Björk" in set_pairs
        assert "album=Homogenic" in set_pairs

    def test_mbid_with_unknown_recording_still_pins_search_id(self, config, tmp_path):
        """A failed lookup must not drop the pin: beets still gets
        --search-id mbid, just without the --set album fields."""
        mb = FakeMusicBrainz(recording=None)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False, library=True, mbid="missing-1")

        argv = run.call_args.args[0]
        assert argv[argv.index("--search-id") + 1] == "missing-1"
        assert "--set" not in argv
        assert mb.lookup_calls == ["missing-1"]

    def test_default_no_library_no_mbid_is_unchanged(self, config, tmp_path):
        """The plain manual path (no library, no mbid) still routes to
        searches and does not resolve anything."""
        mb = FakeMusicBrainz(recording=None)
        service = BeetsService(config, musicbrainz_service=mb)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False)

        argv = run.call_args.args[0]
        assert argv[1:3] == ["--config", str(tmp_path / "data/beets/searches.yaml")]
        assert "--search-id" not in argv
        assert mb.lookup_calls == []
        assert mb.calls == []


class TestResultReporting:
    def test_relative_library_path_is_resolved_against_the_tree(self, config, tmp_path):
        """beets 2.x stores items.path relative to its `directory` (verified
        against beets 2.13.1 in the built image) — an unresolved relative
        path would be reported to the caller and fail to exist."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/01 Track.mp3",
                100.0,
                mb_trackid="mbid-1",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok
        assert result.matched is True
        assert result.target_path == landed

    def test_absolute_library_path_is_passed_through(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                str(landed),
                100.0,
                mb_trackid="mbid-1",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.target_path == landed

    def test_unmatched_import_is_reported_not_failed(self, config, tmp_path):
        """P6.6-4: beets imported the file with its own tags (no MBIDs).
        That is a success with matched=False, not a failure."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Unknown/track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db", "Unknown/track.mp3", 100.0
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok
        assert result.matched is False
        assert result.target_path == landed

    def test_reported_path_that_does_not_exist_is_a_failure(self, config, tmp_path):
        """Guards against marking a download 'moved' on beets' word alone —
        the source file would be orphaned and never retried."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        def _run(*_args, **_kwargs):
            _seed_library(tmp_path / "data/beets/searches.db", "Ghost/none.mp3", 100.0)
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert not result.ok
        assert "nothing is there" in result.error

    def test_only_items_added_by_this_import_are_considered(self, config, tmp_path):
        """A pre-existing library row must not be mistaken for the file we
        just imported — otherwise a no-op import reports someone else's
        path and the source file is marked moved while still sitting on
        disk."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        _land(tmp_path, "Old/old.mp3")
        _seed_library(tmp_path / "data/beets/searches.db", "Old/old.mp3", 50.0)

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = service.import_file(source, is_rec=False)

        assert not result.ok
        assert result.target_path is None


class TestFailureModes:
    def test_missing_binary_is_reported_not_raised(self, tmp_path):
        config = _make_config(tmp_path, binary="definitely-not-beet")
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = service.import_file(source, is_rec=False)

        assert not result.ok
        assert "not found" in result.error

    def test_timeout_is_reported_not_raised(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("beet", 120)
        ):
            result = service.import_file(source, is_rec=False)

        assert not result.ok
        assert "timed out" in result.error

    def test_nonzero_exit_is_reported(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
            result = service.import_file(source, is_rec=False)

        assert not result.ok
        assert result.error == "boom"


class TestDuplicateHandling:
    """beets' duplicate guard exits 0 and imports nothing. Treating that as
    a plain failure made the monitor re-run the same import every poll
    cycle forever — live-confirmed 2026-08-11, the same file retried at
    :19:51, :20:14 and :20:46."""

    def _run_with_output(self, stdout):
        def _run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, stdout, "")

        return _run

    def test_duplicate_skip_is_reported_as_terminal(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch(
            "subprocess.run",
            side_effect=self._run_with_output(
                "Importing as-is.\nThis album is already in the library!\nSkipping.\n"
            ),
        ):
            result = service.import_file(source, is_rec=False)

        assert result.duplicate is True
        assert result.ok is False
        assert result.handled is True

    def test_surviving_source_file_means_skipped(self, config, tmp_path):
        """The authoritative duplicate signal, since beets skips silently
        under `duplicate_action: skip`: the profile sets `move: yes`, so a
        real import consumes the source and a skip leaves it untouched."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run", side_effect=self._run_with_output("")):
            result = service.import_file(source, is_rec=False)

        assert result.duplicate is True
        assert result.handled is True

    def test_consumed_source_with_no_item_is_a_genuine_failure(self, config, tmp_path):
        """beets took the file but nothing landed in the library — that is
        a real problem and must stay retryable, not be silently absorbed."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        def _run(*_args, **_kwargs):
            source.unlink()
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.duplicate is False
        assert result.handled is False

    def test_successful_import_is_handled(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        _land(tmp_path, "A/B/1 t.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(tmp_path / "data/beets/searches.db", "A/B/1 t.mp3", 100.0)
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.handled is True
        assert result.duplicate is False

    def test_duplicate_action_skip_is_configured(self, config, tmp_path):
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            service.import_file(source, is_rec=False)

        assert "duplicate_action: skip" in (
            tmp_path / "data/beets/searches.yaml"
        ).read_text()


class TestCrossProfileDuplicates:
    """P6.6-5: beets' own duplicate_action only sees the importing profile's
    library.db — the same recording imported once via discovery_familiar
    and once via searches lands in both trees undetected. import_file now
    checks every other known profile's db by mb_trackid after a successful
    import."""

    def test_duplicate_in_another_profile_is_removed(self, config, tmp_path):
        # discovery already has this recording — row *and* file, since a
        # row alone is not evidence the track is really in the library.
        _seed_library(
            tmp_path / "data/beets/discovery_familiar.db",
            "Artist/Album/01 Track.mp3",
            50.0,
            mb_trackid="mbid-shared",
        )
        _land_in(tmp_path, "discovery/Comfort_Zone", "Artist/Album/01 Track.mp3")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/01 Track.mp3",
                100.0,
                mb_trackid="mbid-shared",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok is False
        assert result.duplicate is True
        assert result.handled is True
        assert not landed.exists(), "the duplicate copy must be deleted"

        # its own (searches) library row must be cleaned up too, not left dangling.
        with sqlite3.connect(str(tmp_path / "data/beets/searches.db")) as conn:
            row = conn.execute(
                "SELECT 1 FROM items WHERE mb_trackid = ?", ("mbid-shared",)
            ).fetchone()
        assert row is None

    def test_different_recording_in_another_profile_is_not_a_duplicate(
        self, config, tmp_path
    ):
        _seed_library(
            tmp_path / "data/beets/discovery_familiar.db",
            "Artist/Album/01 Other.mp3",
            50.0,
            mb_trackid="mbid-other",
        )

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/02 Track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/02 Track.mp3",
                100.0,
                mb_trackid="mbid-this-one",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok is True
        assert result.duplicate is False
        assert result.target_path == landed
        assert landed.exists()

    def test_unmatched_import_never_cross_profile_matches(self, config, tmp_path):
        """Two unmatched (asis) imports both have NULL mb_trackid — that
        must never be treated as a match against each other."""
        _seed_library(
            tmp_path / "data/beets/discovery_familiar.db",
            "Unknown/other.mp3",
            50.0,
            mb_trackid=None,
        )

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Unknown/track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Unknown/track.mp3",
                100.0,
                mb_trackid=None,
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok is True
        assert result.duplicate is False
        assert landed.exists()

    def test_only_other_profiles_are_checked_not_this_one(self, config, tmp_path):
        """A same-profile match is beets' own duplicate_action's job (and it
        already skips silently before a new row even exists) — the
        cross-profile check must not treat this profile's own fresh row as
        a match against itself."""
        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/01 Track.mp3",
                100.0,
                mb_trackid="mbid-solo",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok is True
        assert result.duplicate is False
        assert landed.exists()


class TestStaleLibraryRows:
    """2026-08-12 blocker. Both duplicate guards — beets' own
    `duplicate_action: skip` and the cross-profile check above — answer
    "already in the library?" from a library row alone. When the music tree
    is restructured or files are removed outside beets, those rows outlive
    their files and start rejecting downloads of tracks that are *not* in
    the library, stranding the file in downloads/ with nothing to retry.
    Live-confirmed: 49 of 50 rows across the two profiles were dead and four
    completed downloads were stuck behind them.
    """

    def test_skip_backed_by_a_missing_file_prunes_and_retries(
        self, config, tmp_path
    ):
        """The whole blocker in one test: beets refuses the import because
        its library claims the track, but the claimed file is gone. The
        stale row must be dropped and the import must actually happen."""
        library_db = tmp_path / "data/beets/searches.db"
        _seed_library(library_db, "Artist/Album/01 Track.mp3", 50.0, "mbid-stale")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        calls = []

        def _run(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                # First attempt: beets skips, source survives untouched.
                return subprocess.CompletedProcess([], 0, "", "")
            # Second attempt, after the dead row is gone: real import.
            source.unlink()
            _land(tmp_path, "Artist/Album/01 Track.mp3")
            _seed_library(
                library_db, "Artist/Album/01 Track.mp3", 100.0, "mbid-stale"
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert len(calls) == 2, "the import was not retried after pruning"
        assert result.ok is True
        assert result.duplicate is False
        assert result.target_path == tmp_path / "searches/Artist/Album/01 Track.mp3"
        assert _item_count(library_db) == 1, "the dead row must be gone"

    def test_skip_backed_by_a_real_file_is_still_a_duplicate(self, config, tmp_path):
        """The guard must keep working when the library is honest — a row
        whose file exists still blocks the import, and beets is run once."""
        library_db = tmp_path / "data/beets/searches.db"
        _seed_library(library_db, "Artist/Album/01 Track.mp3", 50.0, "mbid-real")
        _land(tmp_path, "Artist/Album/01 Track.mp3")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = service.import_file(source, is_rec=False)

        assert run.call_count == 1, "no pruning happened, so no retry should"
        assert result.duplicate is True
        assert result.handled is True
        assert source.exists(), "the redundant download must not be deleted"
        assert _item_count(library_db) == 1

    def test_retry_that_still_skips_reports_a_duplicate(self, config, tmp_path):
        """Pruning is not a licence to loop: exactly one retry, and if beets
        still refuses, the result is a duplicate like before."""
        library_db = tmp_path / "data/beets/searches.db"
        _seed_library(library_db, "Gone/Album/01 Gone.mp3", 50.0, "mbid-a")
        _seed_library(library_db, "Here/Album/01 Here.mp3", 60.0, "mbid-b")
        _land(tmp_path, "Here/Album/01 Here.mp3")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = service.import_file(source, is_rec=False)

        assert run.call_count == 2
        assert result.duplicate is True
        assert result.ok is False
        # Only the row whose file is missing was dropped.
        assert _item_count(library_db) == 1

    def test_healthy_import_does_not_prune(self, config, tmp_path):
        """The existence sweep is on the skip path only — an import that
        lands normally must leave every other row alone, however stale."""
        library_db = tmp_path / "data/beets/searches.db"
        _seed_library(library_db, "Gone/Album/01 Gone.mp3", 50.0, "mbid-gone")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/02 Track.mp3")

        def _run(*_args, **_kwargs):
            source.unlink()
            _seed_library(library_db, "Artist/Album/02 Track.mp3", 100.0, "mbid-new")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.target_path == landed
        assert _item_count(library_db) == 2

    def test_cross_profile_row_without_its_file_is_not_a_duplicate(
        self, config, tmp_path
    ):
        """The other profile claims this recording but its file is gone —
        that must not delete the copy we just imported."""
        _seed_library(
            tmp_path / "data/beets/discovery_familiar.db",
            "Artist/Album/01 Track.mp3",
            50.0,
            mb_trackid="mbid-shared",
        )

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            source.unlink()
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/01 Track.mp3",
                100.0,
                mb_trackid="mbid-shared",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.ok is True, "a dead row must not block a real import"
        assert result.duplicate is False
        assert landed.exists()
        # ...and the dead row is cleaned up so it cannot reject the next one.
        assert _item_count(tmp_path / "data/beets/discovery_familiar.db") == 0

    def test_cross_profile_prune_keeps_rows_whose_file_exists(
        self, config, tmp_path
    ):
        """Two rows for one recording, one dead and one live: the live one
        still wins and the dead one is left alone (the live match short-
        circuits before any deletion)."""
        other_db = tmp_path / "data/beets/discovery_familiar.db"
        _seed_library(other_db, "Artist/Album/01 Live.mp3", 40.0, "mbid-shared")
        _land_in(tmp_path, "discovery/Comfort_Zone", "Artist/Album/01 Live.mp3")

        service = BeetsService(config)
        source = tmp_path / "src" / "track.mp3"
        source.parent.mkdir(parents=True)
        source.write_text("data")
        landed = _land(tmp_path, "Artist/Album/01 Track.mp3")

        def _run(*_args, **_kwargs):
            source.unlink()
            _seed_library(
                tmp_path / "data/beets/searches.db",
                "Artist/Album/01 Track.mp3",
                100.0,
                mb_trackid="mbid-shared",
            )
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("subprocess.run", side_effect=_run):
            result = service.import_file(source, is_rec=False)

        assert result.duplicate is True
        assert not landed.exists()
        assert _item_count(other_db) == 1
