"""
Issue #2 "albums still fracture" (live).

Note: deliberately NOT named/prefixed "S11" — that id is already taken by
Stage.S11_NAVIDROME_INDEXED in probes/contract.py and by
test_stages_library.py::test_s11_scan_reads_every_tree. This file is a
standalone probe of the consolidation engine, not part of the S1-S13
funnel.

Drives the real `BeetsService.import_file` / consolidation code inside the
running `postvinyl` container, same as the S10 dedup scenarios in
test_stages_import.py, but without queueing real Soulseek downloads: two
synthetic "peer files" are freshly generated (unique noise audio, not a
copy of any real donor track) and tagged (via mutagen, inside the
container) to a fresh album identity that exists nowhere in any beets
profile db, so neither MusicBrainz resolution nor the cross-profile-
duplicate guard can interfere with what's actually under test — whether two
separately-imported tracks of the same album end up unified. An earlier
version of this file copied an existing donor track's audio bytes, but real
library files can carry stray/duplicate tag fields (e.g. a FLAC with both
ALBUMARTIST and ALBUM_ARTIST vorbis comments) that survive an easy-tag
overwrite and made beets resolve the synthetic file back to the donor's own
real album as "already in library" — fresh audio has neither problem.

Test plan posted to https://github.com/Goober-nation/Post-Vinyl/issues/2

Requires: `docker compose up -d` with the postvinyl container healthy.

Run just this file:
    python3 -m pytest tests/live/test_album_consolidation.py --live -v
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.live.harness import REPO_ROOT

pytestmark = [pytest.mark.no_downloads]

_JSON_SENTINEL = "@@ALBUM_CONSOLIDATION_JSON@@"
_SERVICE = "postvinyl"
_PROFILES = (
    "searches",
    "library",
    "discovery_familiar",
    "discovery_new_releases",
    "discovery_exploration",
)

_PREAMBLE = f"""
import json, os, shutil, sys
from pathlib import Path
from app.config import Config
_cfg = Config(os.environ.get("MUSICA_CONFIG_PATH"))
_cfg.load()
def _emit(payload):
    print("{_JSON_SENTINEL}" + json.dumps(payload, default=str))
"""

_STAGE_AND_TAG_DRIVER = (
    _PREAMBLE
    + """
import random
import wave
from mutagen.id3 import TALB, TIT2, TPE1, TPE2, TRCK
from mutagen.wave import WAVE

dst = Path(sys.argv[1])
albumartist = sys.argv[2]
album = sys.argv[3]
title = sys.argv[4]
track_no = int(sys.argv[5])
# A per-track seed keeps the generated audio distinct from anything else
# ever imported (a donor's real audio bytes carry the donor's own identity
# via duplicate content, and messy source tagging can leave stray fields —
# e.g. a FLAC with both ALBUMARTIST and ALBUM_ARTIST vorbis comments — that
# survive an easy-tag overwrite and resolve back to the real album. Fresh,
# uniquely-seeded silence with only the tags this test wrote has neither
# problem.)
seed = int(sys.argv[6])

dst.parent.mkdir(parents=True, exist_ok=True)
rng = random.Random(seed)
with wave.open(str(dst), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    frames = bytes(rng.getrandbits(8) for _ in range(8000))  # 1s of noise
    w.writeframes(frames)

audio = WAVE(dst)
audio.add_tags()
audio.tags.add(TPE2(encoding=3, text=[albumartist]))  # albumartist
audio.tags.add(TPE1(encoding=3, text=[albumartist]))  # artist
audio.tags.add(TALB(encoding=3, text=[album]))
audio.tags.add(TIT2(encoding=3, text=[title]))
audio.tags.add(TRCK(encoding=3, text=[str(track_no)]))
audio.save()

_emit({"dst": str(dst), "size": dst.stat().st_size})
"""
)

_IMPORT_DRIVER = (
    _PREAMBLE
    + """
from app.services.beets import BeetsService
source = Path(sys.argv[1])
svc = BeetsService(_cfg)
result = svc.import_file(source, is_rec=False)
_emit({
    "source": str(source),
    "matched": bool(result.matched),
    "target_path": str(result.target_path) if result.target_path else None,
    "error": result.error,
    "duplicate": bool(result.duplicate),
    "ok": bool(result.ok),
})
"""
)

_CONSOLIDATE_DRIVER = (
    _PREAMBLE
    + """
from app.services.beets import BeetsService
svc = BeetsService(_cfg)
summary = svc.consolidate_all()
_emit(summary)
"""
)

_RESOLVE_CANONICAL_DRIVER = (
    _PREAMBLE
    + """
from app.services.musicbrainz_client import MusicBrainzClient
title = sys.argv[1]
artist = sys.argv[2]
client = MusicBrainzClient(_cfg)
recording = client.resolve_canonical(title, artist, min_score=_cfg.musicbrainz.min_score)
_emit({
    "resolved": recording is not None,
    "mbid": getattr(recording, "mbid", None),
    "artist": getattr(recording, "artist", None),
    "artist_credit": getattr(recording, "artist_credit", None),
    "title": getattr(recording, "title", None),
})
"""
)

_RMTREE_DRIVER = (
    _PREAMBLE
    + """
removed = []
for raw in sys.argv[1:]:
    p = Path(raw)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True); removed.append(str(p))
    elif p.exists():
        p.unlink(missing_ok=True); removed.append(str(p))
_emit({"removed": removed})
"""
)


def compose_exec_python(script: str, *args: str, timeout: float = 120.0) -> dict:
    """Run a driver inside the real postvinyl container and return its JSON
    payload. Self-contained (not reusing test_stages_import.py's version)
    since that one lives in a different, much larger file focused on the
    S7-S10 funnel — small deliberate duplication rather than a new shared
    import."""
    cmd = ["docker", "compose", "exec", "-T", _SERVICE, "python3", "-c", script, *args]
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout,
        check=False,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(_JSON_SENTINEL):
            return json.loads(line[len(_JSON_SENTINEL):])
    raise AssertionError(
        "in-container driver produced no result\n"
        f"exit={proc.returncode}\nstdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )


def _beets_db_path(profile: str) -> Path:
    return REPO_ROOT / "app_data" / "beets" / f"{profile}.db"


def _rows_for_album(albumartist: str, album: str) -> list[dict]:
    """Every item across every profile db matching this album identity —
    the host-side bind-mounted copies, no container exec needed for a read."""
    out: list[dict] = []
    for profile in _PROFILES:
        db_path = _beets_db_path(profile)
        if not db_path.exists():
            continue
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, path, albumartist, album, title, track, "
                "mb_trackid, mb_albumid FROM items "
                "WHERE albumartist = ? AND album = ?",
                (albumartist, album),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        for r in rows:
            d = dict(r)
            d["profile"] = profile
            out.append(d)
    return out


@dataclass
class SyntheticAlbum:
    albumartist: str
    album: str
    staging_container_dir: str
    _seed_counter: int = 0

    def stage_track(self, title: str, track_no: int) -> dict:
        """Generate a fresh, never-before-imported audio file (unique
        per-track noise, not a copy of any real donor — see
        _STAGE_AND_TAG_DRIVER's docstring for why a real donor's bytes or
        stray inherited tags make beets treat it as a duplicate of the
        original recording) and tag it as this album."""
        self._seed_counter += 1
        dst = f"{self.staging_container_dir}/{track_no:02d} {title}.wav"
        return compose_exec_python(
            _STAGE_AND_TAG_DRIVER,
            dst, self.albumartist, self.album, title, str(track_no),
            str(hash((self.album, title, self._seed_counter)) & 0xFFFFFFFF),
        )


@pytest.fixture
def synthetic_album() -> Iterator[SyntheticAlbum]:
    """A fresh, never-before-seen album identity + a synthetic slskd peer
    directory to stage tracks under. Cleaned up unconditionally."""
    tag = uuid.uuid4().hex[:8]
    peer = f"s11-synthetic-{tag}"
    album = SyntheticAlbum(
        albumartist=f"S11 Test Artist {tag}",
        album=f"S11 Test Album {tag}",
        staging_container_dir=f"/music/downloads/complete/soulseek/{peer}",
    )
    before_added = {p: _max_added(p) for p in _PROFILES}
    try:
        yield album
    finally:
        compose_exec_python(_RMTREE_DRIVER, album.staging_container_dir)
        for profile, before in before_added.items():
            _prune_since(profile, before)
        # Any item that got moved into a profile's home tree during the
        # test needs its files removed too — sweep by album identity.
        for row in _rows_for_album(album.albumartist, album.album):
            pass  # rows are pruned by _prune_since above; files under the
            # profile tree are orphaned dirs at worst, harmless for a test
            # album name that will never recur.


def _max_added(profile: str) -> float:
    db_path = _beets_db_path(profile)
    if not db_path.exists():
        return 0.0
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT MAX(added) AS m FROM items").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return (row[0] if row and row[0] else 0.0) or 0.0


def _prune_since(profile: str, after_added: float) -> int:
    db_path = _beets_db_path(profile)
    if not db_path.exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute("DELETE FROM items WHERE added > ?", (after_added,))
        con.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


class TestSequentialImportRace:
    """A track imports alone (group size 1, no mbid) — does a SECOND track
    of the same album, imported afterward, retroactively pull the first one
    in, or does it stay stranded until a manual consolidate_all() sweep?"""

    def test_second_track_of_the_album_unifies_with_the_first(
        self, synthetic_album
    ):
        staged_a = synthetic_album.stage_track("Track A", 1)
        result_a = compose_exec_python(_IMPORT_DRIVER, staged_a["dst"])
        assert result_a["ok"], result_a
        rows_after_a = _rows_for_album(
            synthetic_album.albumartist, synthetic_album.album
        )
        assert len(rows_after_a) == 1
        profile_a = rows_after_a[0]["profile"]

        staged_b = synthetic_album.stage_track("Track B", 2)
        result_b = compose_exec_python(_IMPORT_DRIVER, staged_b["dst"])
        assert result_b["ok"], result_b

        rows_after_b = _rows_for_album(
            synthetic_album.albumartist, synthetic_album.album
        )
        assert len(rows_after_b) == 2, (
            "expected both tracks present after the second import",
            rows_after_b,
        )
        profiles = {r["profile"] for r in rows_after_b}
        assert profiles == {profile_a}, (
            "Sequential-import-race reproduction: the two tracks of the same album ended up "
            "in different profile trees instead of being unified into one "
            "album home — this is issue #2 (\"albums still fracture\") "
            f"live: {rows_after_b}"
        )


class TestMetadataSpellingMismatch:
    """Same two-track setup, but the albumartist casing differs between the
    two files (no mbid on either, so grouping falls back to normalized
    string bucketing) — does that alone fracture the album?"""

    def test_mismatched_albumartist_casing_still_unifies(
        self, synthetic_album
    ):
        tag = synthetic_album.album.rsplit(" ", 1)[-1]
        variant_artist = synthetic_album.albumartist.upper()

        staged_a = synthetic_album.stage_track("Track A", 1)
        result_a = compose_exec_python(_IMPORT_DRIVER, staged_a["dst"])
        assert result_a["ok"], result_a

        # Second track: same album title, but SHOUTED albumartist casing.
        dst_b = f"{synthetic_album.staging_container_dir}/02 Track B.wav"
        stage_b = compose_exec_python(
            _STAGE_AND_TAG_DRIVER,
            dst_b, variant_artist, synthetic_album.album, "Track B", "2",
            str(hash((synthetic_album.album, "Track B", "variant")) & 0xFFFFFFFF),
        )
        result_b = compose_exec_python(_IMPORT_DRIVER, stage_b["dst"])
        assert result_b["ok"], result_b

        rows = _rows_for_album(synthetic_album.albumartist, synthetic_album.album)
        rows += [
            r for r in _rows_for_album(variant_artist, synthetic_album.album)
        ]
        distinct_artists = {r["albumartist"] for r in rows}
        assert len(rows) == 2, rows
        assert len(distinct_artists) == 1, (
            "Metadata-spelling-mismatch reproduction: a mere albumartist casing difference "
            f"between the two tracks left them under separate spellings "
            f"instead of converging to one canonical artist string: {rows}"
        )


class TestSweepParity:
    """Whatever state the per-import hook leaves the album in, does running
    the one-shot consolidate_all() sweep afterward converge it further? If
    so, that's the parity gap: the hook and the sweep don't agree, which is
    exactly why albums that fracture on import stay fractured until someone
    remembers to run a manual sweep."""

    def test_consolidate_all_does_not_change_an_already_converged_album(
        self, synthetic_album
    ):
        """Baseline: if S11a passes (the hook already unifies), the sweep
        must be a no-op on top of it — confirms the sweep and the hook
        agree on the converged state, not just that both eventually get
        there by different paths."""
        staged_a = synthetic_album.stage_track("Track A", 1)
        compose_exec_python(_IMPORT_DRIVER, staged_a["dst"])
        staged_b = synthetic_album.stage_track("Track B", 2)
        compose_exec_python(_IMPORT_DRIVER, staged_b["dst"])

        before = _rows_for_album(synthetic_album.albumartist, synthetic_album.album)
        compose_exec_python(_CONSOLIDATE_DRIVER)
        after = _rows_for_album(synthetic_album.albumartist, synthetic_album.album)

        before_profiles = {r["profile"] for r in before}
        after_profiles = {r["profile"] for r in after}
        assert len(after) == len(before), (before, after)
        assert after_profiles == before_profiles, (
            "consolidate_all() moved an already-converged album — the "
            "sweep and the per-import hook disagree on the converged "
            f"state: before={before} after={after}"
        )


class TestResolveCanonicalArtistMismatch:
    """The known "Abstract Orchestra" defect (live-verified 2026-08-12,
    see agents_memory): MusicBrainzClient.resolve_canonical can match a
    cover band's recording of the same title instead of the original
    artist's, because the query is title-first with insufficient artist
    discrimination — a wrong-but-confident match, worse than no match,
    since it silently mistags the file with someone else's album identity
    and can fracture an otherwise-correct album grouping. Hits the real
    MusicBrainz API (rate-limited to musicbrainz.min_request_interval), so
    this is slow and network-dependent by nature."""

    def test_madvillain_all_caps_does_not_resolve_to_a_cover_band(self):
        result = compose_exec_python(
            _RESOLVE_CANONICAL_DRIVER, "All Caps", "Madvillain", timeout=60.0
        )
        if not result["resolved"]:
            pytest.skip(
                "MusicBrainz returned no confident match this run — cannot "
                "exercise the mismatch path without a resolved recording"
            )
        assert "abstract orchestra" not in (result["artist"] or "").lower(), (
            "resolve_canonical-mismatch reproduction: resolve_canonical matched 'All Caps' to "
            f"Abstract Orchestra's cover recording instead of Madvillain's "
            f"original — {result}"
        )
        assert "madvillain" in (result["artist"] or "").lower() or (
            "madvillain" in (result["artist_credit"] or "").lower()
        ), result
