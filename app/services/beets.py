"""
BeetsService — invokes beets (`beet import`) as a subprocess to tag, rename,
and place completed downloads.

Replaces DownloadMonitor._move_file(), which had two real defects: it
globbed for the first basename match (wrong file when two downloads share a
name), and shutil.move across Docker volumes is copy-then-delete (a
container kill mid-move leaves a partial file for Navidrome to scan). beets
matches on the exact source path we hand it and does its own move/rename
internally — neither defect applies here.

One beets profile (config + library db) per target tree. P6.7-0b grew the
set from 2 to 5: "searches" (manual downloads), "library" (MusicBrainz-
sourced — scaffolded slot, receives no real downloads until Phase 6.8),
and one profile per rec category — "discovery_familiar" (Comfort Zone),
"discovery_new_releases" (Fresh Picks), "discovery_exploration" (Deep
Cuts) — each a subdirectory of the Discovery tree. Routing is driven by
`recommendations.source` threaded through `import_file`'s `category`
argument. The old merged "discovery" profile is gone (user decision
2026-08-13: no fallback profile exists; a rec whose category can't be
resolved fails the import rather than guessing where it belongs).

P6.6-5 (2026-08-12): beets' own `duplicate_action: skip` only sees the
importing profile's own library.db, so the same track downloaded via two
different profiles (a rec vs. a manual search, say) imports into both trees
undetected — checked how soulbeet handles this and it has the same
per-folder-DB blind spot, no existing pattern to copy. `import_file` now
does a post-import cross-profile check by `mb_trackid` (the only
per-recording-safe key here — `mb_albumid` is deliberately not used alone,
since two different tracks off the same album share it) and deletes the
just-moved duplicate rather than leaving a second copy on disk.

**Stale library rows (2026-08-12).** Both duplicate checks — beets' own and
the cross-profile one above — answer "is this already in the library?"
from a library.db row alone. A row whose file no longer exists (the music
tree was restructured by hand, a volume moved, files deleted outside beets)
therefore blocks the import of a track that is *not* in the library, and the
download is stranded in `downloads/complete/soulseek/` with nothing to
retry. Live-confirmed 2026-08-12: 49 of 50 rows across the two profiles
pointed at files that were gone, and four completed downloads were stuck
behind them. A library row is now only believed when the file it names is
still on disk; rows that fail that test are pruned and the import is
retried once. `tests/live/tools/reconcile_beets.py` does the same sweep
over the whole library as a standalone operation.
"""

import collections
import re
import sqlite3
import subprocess
import unicodedata
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from app.logging_config import get_logger
from app.services.interfaces.musicbrainz import MusicBrainzService
from app.services.musicbrainz_client import MusicBrainzClient

logger = get_logger(__name__)

# Every profile BeetsService currently manages. Used by the cross-profile
# duplicate check to know which other library.db files to consult after an
# import. The old merged "discovery" profile was dropped 2026-08-13 (P6.7-0b)
# — there is deliberately no fallback destination.
_PROFILES = (
    "searches",
    "library",
    "discovery_familiar",
    "discovery_new_releases",
    "discovery_exploration",
)

# recommendations.source -> beets profile. An unknown/empty source has no
# entry on purpose: with no fallback profile, such a rec fails the import.
_CATEGORY_PROFILES = {
    "comfort_zone": "discovery_familiar",
    "fresh_picks": "discovery_new_releases",
    "deep_cuts": "discovery_exploration",
}

_PROFILE_CONFIG_TEMPLATE = """\
directory: {directory}
library: {library}

import:
    move: yes
    quiet: yes
    quiet_fallback: asis
    resume: no
    incremental: no
    write: yes
    duplicate_action: skip
    log: {log_path}

paths:
    default: $albumartist/$album/$track $title
    singleton: $albumartist/$album/$track $title
    comp: Compilations/$album/$track $title
"""


# beets' wording when its duplicate guard refuses an import. Only a hint:
# with `duplicate_action: skip` it skips *silently*, and its messages go
# through a TTY-aware formatter, so this is a secondary signal at best. The
# authoritative check is whether the source file survived — see
# `import_file`.
_DUPLICATE_MARKERS = ("already in the library", "skipping")

# The item columns the consolidation engine reads. `path` is a BLOB (and
# relative to the profile's `directory` in beets 2.x — see `_item_path`).
_ITEM_COLUMNS = (
    "id",
    "path",
    "albumartist",
    "album",
    "title",
    "track",
    "disc",
    "mb_trackid",
    "mb_albumid",
    "added",
)

_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Directory boundaries for empty-dir pruning: a consolidation never removes
# one of these roots, only directories below them.
_CONSOLIDATION_ROOTS = (
    "searches",
    "library",
    "discovery_familiar",
    "discovery_new_releases",
    "discovery_exploration",
)


@dataclass
class _AlbumMember:
    """One live library item plus the profile it lives in.

    `path` is resolved to an absolute filesystem path; `mb_trackid` /
    `mb_albumid` are None when empty (beets stores '' for missing).
    """

    profile: str
    item_id: int
    path: Path
    albumartist: str | None
    album: str | None
    title: str | None
    track: int | None
    mb_trackid: str | None
    mb_albumid: str | None
    added: float


@dataclass
class _AlbumGroup:
    """Every live member of one album identity, and the canonical identity
    `_canonicalize` derives for it."""

    members: list[_AlbumMember]
    canonical_mbid: str | None = None
    canonical_artist: str | None = None
    canonical_title: str | None = None
    # Release tracklist positions when the canonical release is known:
    # recording MBID -> position, and normalized-title -> position (only
    # titles unique on the release, so a match is never ambiguous).
    release_by_mbid: dict[str, int] = field(default_factory=dict)
    release_by_title: dict[str, int] = field(default_factory=dict)
    home_profile: str | None = None


@dataclass
class _MemberMove:
    """Outcome of moving one member into another profile's tree."""

    member: _AlbumMember | None = None
    deduplicated: bool = False


@dataclass
class BeetsImportResult:
    """Outcome of importing a single completed download through beets."""

    matched: bool
    target_path: Path | None
    error: str | None = None
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.target_path is not None

    @property
    def handled(self) -> bool:
        """True when there is nothing further to try for this download.

        A duplicate is *not* a success (nothing moved) but it is terminal —
        retrying it just re-runs the same skip forever.
        """
        return self.ok or self.duplicate


class BeetsService:
    """Runs `beet import` as a subprocess, one profile per target tree."""

    def __init__(
        self, config, musicbrainz_service: MusicBrainzService | None = None
    ) -> None:
        self._config = config
        self._profiles_dir = Path(config.paths.data_dir) / "beets"
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        # P-MB-1 wiring: constrains the match to what the user actually
        # asked for instead of whatever identity the peer's file claims.
        # Injectable for tests; defaults to the real HTTP client.
        self._musicbrainz = musicbrainz_service or MusicBrainzClient(config)

    def import_file(
        self,
        source: Path,
        is_rec: bool,
        title: str | None = None,
        artist: str | None = None,
        category: str | None = None,
        *,
        library: bool = False,
        mbid: str | None = None,
    ) -> BeetsImportResult:
        """
        Import a single completed download file through beets.

        Args:
            source: full path to the file as slskd reported it, already
                confirmed to exist on disk.
            is_rec: True routes into a per-category discovery tree, False
                into searches.
            title, artist: what the user actually asked for (the search or
                recommendation that produced this download), if known. When
                both are given, MusicBrainz is asked to resolve them to a
                canonical studio recording (P-MB-1) and the match is pinned
                to it via `--search-id` — see `_resolve_recording`. Callers
                that can't supply these (or a caller that omits either) get
                the pre-P-MB-1 behavior: beets matches on the peer's own
                tags.
            category: the rec category the download originated from
                (`recommendations.source`: comfort_zone, fresh_picks,
                deep_cuts). Decides which discovery profile the file lands
                in. There is no fallback — a rec with an unresolvable
                category fails the import (user decision 2026-08-13, P6.7-0b)
                rather than guessing a destination; a manual download
                (is_rec=False) always routes to searches regardless.
            library: True routes into the "library" profile regardless of
                is_rec/category (P6.8 MusicBrainz-initiated downloads).
            mbid: an exact MusicBrainz recording MBID to pin the match to.
                When given, it is looked up via `lookup_recording` and
                threaded into `_run_beet_import` as `--search-id` (plus
                `--set` album fields when the lookup returns a recording);
                `resolve_canonical` is *not* run. The pin survives a failed
                lookup — beets still gets `--search-id mbid`, just without
                the `--set` album fields. The MBID is authoritative, so
                title/artist (if any) are ignored.

        Returns:
            BeetsImportResult. `target_path` is None on any failure (beets
            missing, timeout, non-zero exit, unresolvable category, or the
            imported item can't be located afterward) — callers should treat
            that as "not moved" and leave the source file alone, exactly
            like a failed `_move_file()` call.
        """
        if library:
            profile = "library"
        elif not is_rec:
            profile = "searches"
        elif category in _CATEGORY_PROFILES:
            profile = _CATEGORY_PROFILES[category]
        else:
            msg = (
                "rec download has no resolvable category "
                f"({category!r}); no fallback profile exists"
            )
            logger.error("%s (source=%s)", msg, source)
            return BeetsImportResult(False, None, msg)

        target_dir = self._profile_directory(profile)
        target_dir.mkdir(parents=True, exist_ok=True)
        library_db = self._profiles_dir / f"{profile}.db"
        cfg_path = self._write_profile_config(profile, target_dir, library_db)
        search_id: str | None
        if mbid:
            recording = self._musicbrainz.lookup_recording(mbid)
            search_id = mbid
        else:
            recording = self._resolve_recording(title, artist)
            search_id = recording.mbid if recording else None

        before_added = self._max_added(library_db)
        output, failure = self._run_beet_import(cfg_path, source, search_id, recording)
        if failure is not None:
            return failure

        item = self._latest_item(library_db, before_added, target_dir)

        if item is None and self._looks_like_skip(source, output or ""):
            # beets refused the import because its own library already
            # claims this track. That claim is only as good as the library
            # db: a row pointing at a file that no longer exists must not
            # block a real download. Drop those rows and give the import one
            # more go — this is the self-heal path for a library that has
            # drifted from disk. Only reached on the skip path, so a healthy
            # import never pays for the full-table existence sweep.
            pruned = self._prune_missing_items(library_db, target_dir)
            if pruned:
                logger.warning(
                    "beets skipped %s as a duplicate, but %d row(s) in the "
                    "'%s' library pointed at files that no longer exist; "
                    "pruned them and retrying the import",
                    source.name,
                    pruned,
                    profile,
                )
                before_added = self._max_added(library_db)
                output, failure = self._run_beet_import(
                    cfg_path, source, search_id, recording
                )
                if failure is not None:
                    return failure
                item = self._latest_item(library_db, before_added, target_dir)

        if item is None:
            # beets exits 0 whether it imported, skipped a duplicate, or
            # did nothing at all, so "no new library item" is ambiguous.
            # The reliable discriminator is the source file: the profile
            # sets `move: yes`, so a real import consumes it and a skip
            # leaves it exactly where it was. (Message text is not usable —
            # with `duplicate_action: skip` beets skips silently, and its
            # output is TTY-formatted. Live-verified 2026-08-11: the same
            # import printed "already in the library" in a shell and
            # nothing at all under subprocess capture.)
            if self._looks_like_skip(source, output or ""):
                logger.info(
                    "beets skipped %s (already in the library); leaving the "
                    "redundant download in place",
                    source.name,
                )
                return BeetsImportResult(False, None, "already in library", True)
            msg = "beet import exited 0 but no new library item was found"
            logger.warning("%s (source=%s)", msg, source)
            return BeetsImportResult(False, None, msg)

        target_path, matched, mb_trackid = item
        if not target_path.exists():
            msg = f"beets reported {target_path} but nothing is there"
            logger.warning("%s (source=%s)", msg, source)
            return BeetsImportResult(False, None, msg)

        dup_profile = self._find_cross_profile_duplicate(profile, mb_trackid)
        if dup_profile is not None:
            logger.info(
                "beets imported %s into '%s' but it already exists in '%s' "
                "(mb_trackid=%s); removing the duplicate copy",
                source.name,
                profile,
                dup_profile,
                mb_trackid,
            )
            target_path.unlink(missing_ok=True)
            self._delete_since(library_db, before_added)
            return BeetsImportResult(
                False, None, f"duplicate of an item already in '{dup_profile}'", True
            )

        # P6.9-x: album consolidation. The just-imported item joins its
        # album's canonical home — which may retag it to the canonical
        # spelling, renumber it from the MusicBrainz tracklist, move it into
        # another profile's tree (a manual download joining a `library`
        # album, say), or delete it as a same-recording duplicate of an
        # existing copy. All of this is best-effort: a failure leaves the
        # item exactly where import put it, and the repair sweep
        # (consolidate_all) can retry it later.
        try:
            final_path, deduplicated = self._unify_import(
                profile, before_added, target_path
            )
        except Exception:
            logger.warning(
                "album consolidation failed for %s; leaving it where it is",
                source,
                exc_info=True,
            )
            final_path, deduplicated = target_path, False

        if deduplicated:
            return BeetsImportResult(False, None, "already in library", True)
        target_path = final_path

        logger.info(
            "beets imported %s -> %s (matched=%s)", source.name, target_path, matched
        )
        return BeetsImportResult(matched, target_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _profile_directory(self, profile: str) -> Path:
        """The target tree a profile imports into, read from live config so
        a hot-reloaded `paths.*` takes effect on the next import."""
        paths = self._config.paths
        return {
            "searches": paths.searches_path,
            "library": paths.library_path,
            "discovery_familiar": paths.discovery_familiar_path,
            "discovery_new_releases": paths.discovery_new_releases_path,
            "discovery_exploration": paths.discovery_exploration_path,
        }[profile]

    def _resolve_recording(self, title: str | None, artist: str | None):
        """Resolve what the user asked for to a canonical MusicBrainz recording.

        Returns None when either `title` or `artist` is missing (no intent
        to resolve against) or when MusicBrainz has no confident match —
        both are legitimate and fall back to letting beets match on the
        file's own tags, same as before P-MB-1.
        """
        if not title or not artist:
            return None
        min_score = getattr(getattr(self._config, "musicbrainz", None), "min_score", 90)
        return self._musicbrainz.resolve_canonical(title, artist, min_score=min_score)

    def _run_beet_import(
        self,
        cfg_path: Path,
        source: Path,
        search_id: str | None = None,
        recording=None,
    ) -> tuple[str | None, BeetsImportResult | None]:
        """Run one `beet import`.

        Returns `(combined stdout+stderr, None)` when beets ran to a zero
        exit, or `(None, failure_result)` when it could not be run at all or
        exited non-zero. Exactly one element is ever non-None.

        `search_id`, when given, pins the match to that MusicBrainz
        recording via `--search-id` and forces `--from-scratch` so the
        peer's own embedded tags can't override the pinned match — the
        entire point of resolving it in the first place.

        `recording`, when given, also forces `albumartist` (and `album`,
        when a release is known) via `--set`. Singleton mode (`-s`, below)
        matches on the MusicBrainz *recording*, not a release, so beets'
        own `TrackInfo` never carries album-level fields at all — verified
        against beets 2.13.1's `autotag/hooks.py`: only `AlbumInfo` maps
        `artist` -> `albumartist`, and `TrackMatch.apply_metadata` applies
        `TrackInfo.item_data` with no album fallback. A confidently
        resolved recording still leaves albumartist/album blank without
        this — live-verified 2026-08-12 on Björk - Jóga (MBID resolved,
        tags empty).
        """
        set_fields: list[str] = []
        if recording is not None:
            set_fields.append(f"albumartist={recording.artist}")
            best_release = recording.best_release
            if best_release is not None:
                set_fields.append(f"album={best_release.title}")
        try:
            cmd = self._beet_import_command(cfg_path, source, search_id, set_fields)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._config.beets.timeout_seconds,
            )
        except FileNotFoundError:
            msg = f"beets binary not found: {self._config.beets.binary}"
            logger.error(msg)
            return None, BeetsImportResult(False, None, msg)
        except subprocess.TimeoutExpired:
            msg = (
                f"beet import timed out after "
                f"{self._config.beets.timeout_seconds}s: {source}"
            )
            logger.error(msg)
            return None, BeetsImportResult(False, None, msg)

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "beet import failed").strip()
            logger.warning("beet import failed for %s: %s", source, msg)
            return None, BeetsImportResult(False, None, msg)

        return f"{result.stdout or ''}\n{result.stderr or ''}", None

    def _beet_import_command(
        self,
        cfg_path: Path,
        source: Path,
        search_id: str | None = None,
        set_fields: list[str] | None = None,
    ) -> list[str]:
        """Build the `beet import` argv shared by the download path
        (`_run_beet_import`) and album consolidation (`_move_member_to_profile`).

        Singleton mode: musica downloads one track at a time, and beets'
        default album mode matches that lone file against a whole release —
        the 15 "missing tracks" of a 16-track album push the distance to
        ~0.60, far past the auto-accept threshold, so *every* import fell
        back to `asis` with no MusicBrainz data at all. Matching the same
        file as a singleton scores 100% and writes a real recording MBID
        (both live-verified 2026-08-11 on Kendrick Lamar - Alright).

        `search_id` pins the match to that recording via `--search-id` and
        forces `--from-scratch`; `set_fields` are extra `--set FIELD=value`
        entries (albumartist/album/track) applied even when beets falls back
        to asis.
        """
        cmd = [
            self._config.beets.binary,
            "--config",
            str(cfg_path),
            "import",
            "-q",
            "-s",
        ]
        if search_id:
            cmd += ["--search-id", search_id, "--from-scratch"]
        for entry in set_fields or []:
            cmd += ["--set", entry]
        cmd.append(str(source))
        return cmd

    @staticmethod
    def _looks_like_skip(source: Path, output: str) -> bool:
        """Did beets leave the file alone rather than import it?

        `move: yes` means a real import consumes the source, so the source
        still being there is the authoritative signal; the message markers
        are a fallback for the (rare) case where the file was consumed but
        no row landed.
        """
        haystack = output.lower()
        return source.exists() or any(m in haystack for m in _DUPLICATE_MARKERS)

    def _write_profile_config(
        self, profile: str, target_dir: Path, library_db: Path
    ) -> Path:
        """(Re)write the per-profile beets config — cheap, keeps it in sync
        with current paths.* config on every import rather than caching a
        possibly-stale copy across a hot-reload."""
        cfg_path = self._profiles_dir / f"{profile}.yaml"
        log_path = self._profiles_dir / f"{profile}.log"
        cfg_path.write_text(
            _PROFILE_CONFIG_TEMPLATE.format(
                directory=target_dir, library=library_db, log_path=log_path
            )
        )
        return cfg_path

    def _max_added(self, library_db: Path) -> float:
        """Snapshot the newest `items.added` timestamp before import, so the
        item we just imported can be found afterward without depending on
        beets' stdout in quiet mode."""
        if not library_db.exists():
            return 0.0
        try:
            with closing(sqlite3.connect(str(library_db))) as conn:
                row = conn.execute("SELECT MAX(added) FROM items").fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
        except sqlite3.Error:
            return 0.0

    @staticmethod
    def _item_path(raw_path: object, directory: Path) -> Path:
        """Resolve a beets `items.path` value to a real filesystem path.

        `items.path` is a BLOB, and stored **relative to the library
        directory** in beets 2.x (verified against beets 2.13.1 in the built
        image — its own migration is named `items-relative_path`). Older
        layouts stored an absolute path; those are passed through unchanged.
        """
        if isinstance(raw_path, bytes):
            decoded = raw_path.decode("utf-8", "surrogateescape")
        else:
            decoded = str(raw_path)
        path = Path(decoded)
        return path if path.is_absolute() else directory / path

    def _latest_item(
        self, library_db: Path, after_added: float, target_dir: Path
    ) -> tuple[Path, bool, str | None] | None:
        """Find the item beets just imported (added > snapshot) and whether
        it matched MusicBrainz metadata (mb_trackid/mb_albumid set) or was
        imported as-is (P6.6-4 unmatched handling)."""
        if not library_db.exists():
            return None
        try:
            with closing(sqlite3.connect(str(library_db))) as conn:
                row = conn.execute(
                    "SELECT path, mb_trackid, mb_albumid FROM items "
                    "WHERE added > ? ORDER BY added DESC LIMIT 1",
                    (after_added,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None

        raw_path, mb_trackid, mb_albumid = row
        matched = bool(mb_trackid or mb_albumid)
        return self._item_path(raw_path, target_dir), matched, (mb_trackid or None)

    def _prune_missing_items(self, library_db: Path, directory: Path) -> int:
        """Delete every item row whose file is no longer on disk.

        Returns the number of rows removed. Album rows left with no items
        go too: beets would keep listing them and their `artpath` points
        into a directory that is equally gone.

        Only ever removes *rows*. It never touches files — a row and its
        file disagreeing means the file already went away.
        """
        if not library_db.exists():
            return 0
        try:
            with closing(sqlite3.connect(str(library_db))) as conn:
                rows = conn.execute("SELECT id, path FROM items").fetchall()
                dead = [
                    (item_id,)
                    for item_id, raw_path in rows
                    if not self._item_path(raw_path, directory).exists()
                ]
                if not dead:
                    return 0
                conn.executemany("DELETE FROM items WHERE id = ?", dead)
                conn.commit()
                # Separate transaction on purpose: album cleanup is
                # cosmetic, and losing the item prune because of it would
                # leave the import blocked all over again.
                try:
                    conn.execute(
                        "DELETE FROM albums WHERE id NOT IN "
                        "(SELECT album_id FROM items WHERE album_id IS NOT NULL)"
                    )
                    conn.commit()
                except sqlite3.Error:
                    logger.debug("No album rows to clean up in %s", library_db)
                return len(dead)
        except sqlite3.Error:
            logger.warning(
                "Could not prune missing-file rows from %s", library_db, exc_info=True
            )
            return 0

    def _find_cross_profile_duplicate(
        self, current_profile: str, mb_trackid: str | None
    ) -> str | None:
        """Check every *other* known profile's library.db for the same
        recording (by mb_trackid — the only key here that's safe at
        per-track granularity; mb_albumid alone would false-positive across
        different tracks off the same album).

        Returns the other profile's name if found, else None. Unmatched
        (`asis`) imports have no mb_trackid and are never checked — nothing
        to compare, and NULL must never be treated as a match.

        A matching row only counts when the file it names still exists.
        Rows for this recording that point at nothing are deleted on the
        spot: they are pure garbage, and leaving them means the *next*
        download of the same track gets falsely rejected again.
        """
        if not mb_trackid:
            return None
        for profile in _PROFILES:
            if profile == current_profile:
                continue
            other_db = self._profiles_dir / f"{profile}.db"
            if not other_db.exists():
                continue
            other_dir = self._profile_directory(profile)
            try:
                with closing(sqlite3.connect(str(other_db))) as conn:
                    rows = conn.execute(
                        "SELECT id, path FROM items WHERE mb_trackid = ?",
                        (mb_trackid,),
                    ).fetchall()
                    if not rows:
                        continue
                    dead = []
                    for item_id, raw_path in rows:
                        if self._item_path(raw_path, other_dir).exists():
                            return profile
                        dead.append((item_id,))
                    conn.executemany("DELETE FROM items WHERE id = ?", dead)
                    conn.commit()
                    logger.warning(
                        "'%s' claimed mb_trackid=%s but all %d row(s) point at "
                        "files that no longer exist; removed them instead of "
                        "rejecting the import",
                        profile,
                        mb_trackid,
                        len(dead),
                    )
            except sqlite3.Error:
                continue
        return None

    def _delete_since(self, library_db: Path, after_added: float) -> None:
        """Remove the item(s) added since `after_added` from this profile's
        library — used to clean up a just-imported cross-profile duplicate
        so it doesn't leave a dangling row pointing at a deleted file."""
        try:
            with closing(sqlite3.connect(str(library_db))) as conn:
                conn.execute("DELETE FROM items WHERE added > ?", (after_added,))
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to clean up duplicate item's library row in %s", library_db
            )

    # ------------------------------------------------------------------
    # Album consolidation (P6.9)
    #
    # Albums fracture because musica downloads one track at a time from
    # arbitrary peers: each peer spells the album differently, so singleton
    # imports land in differently-named directories, and — since a manual
    # search routes to the "searches" tree while a MusicBrainz download
    # routes to "library" and a rec to its discovery tree — the same album
    # can be spread across several trees at once (live example 2026-08-14:
    # Terror Reid - Hot Vodka 2 split between library/ and searches/).
    # `_unify_group` regroups every live member of one album identity and:
    #   - dedupes same-recording copies (matched + unmatched same-title
    #     pairs are beets' own duplicate guard blind spot — it matches on
    #     mb_trackid only, so an asis copy sails past);
    #   - moves members into the canonical home tree (`library` if any
    #     member lives there, else the majority tree);
    #   - re-tags every member to the canonical albumartist/album spelling
    #     (the MusicBrainz release title when the release is known, else the
    #     majority spelling), stamps the release `mb_albumid` on every member
    #     that lacks it (so Navidrome — which keeps MBID-bearing files in a
    #     separate album from MBID-less ones — merges the fragments back into
    #     one album), and renumbers tracks from the release tracklist when the
    #     release is known.
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(value: str | None) -> str:
        """Case/punctuation-insensitive album identity key: casefolded,
        non-alphanumerics stripped ('Hot Vodka 2' == 'HOT VODKA 2')."""
        if not value:
            return ""
        ascii_ = unicodedata.normalize("NFKD", value).encode("ascii", "ignore")
        return _ALNUM_RE.sub("", ascii_.decode().lower())

    @staticmethod
    def _valid_mbid(value: str | None) -> str | None:
        """A MusicBrainz ID is a UUID. Peers' files carry lookalike garbage
        (live 2026-08-14: NO GIMMIX with mb_albumid '1yTnNouJawgOy700QENgVh'
        — not a UUID — which isolated it into its own album forever and made
        the release lookup 400). Such a value is None: it must not bucket a
        member into its own album, be chosen as the canonical release, or
        be looked up."""
        if not value:
            return None
        try:
            uuid.UUID(value)
        except ValueError:
            return None
        return value

    def _member_from_row(
        self, profile: str, directory: Path, row: tuple
    ) -> _AlbumMember | None:
        """One items row -> member, or None when the row's file is gone
        (a stale row must not seed an album group)."""
        d = dict(zip(_ITEM_COLUMNS, row))
        path = self._item_path(d["path"], directory)
        if not path.exists():
            return None
        return _AlbumMember(
            profile=profile,
            item_id=d["id"],
            path=path,
            albumartist=d["albumartist"],
            album=d["album"],
            title=d["title"],
            track=d["track"],
            mb_trackid=self._valid_mbid(d["mb_trackid"]),
            mb_albumid=self._valid_mbid(d["mb_albumid"]),
            added=d["added"],
        )

    def _read_row_by_id(self, profile: str, item_id: int) -> dict | None:
        db = self._profiles_dir / f"{profile}.db"
        if not db.exists():
            return None
        try:
            with closing(sqlite3.connect(str(db))) as conn:
                row = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items WHERE id = ?",
                    (item_id,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return dict(zip(_ITEM_COLUMNS, row))

    def _read_member_by_added(
        self, profile: str, before_added: float
    ) -> _AlbumMember | None:
        """The item a just-finished import created (added > snapshot)."""
        db = self._profiles_dir / f"{profile}.db"
        if not db.exists():
            return None
        try:
            with closing(sqlite3.connect(str(db))) as conn:
                row = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items "
                    "WHERE added > ? ORDER BY added DESC LIMIT 1",
                    (before_added,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return self._member_from_row(profile, self._profile_directory(profile), row)

    def _find_group_for(self, seed: _AlbumMember) -> _AlbumGroup:
        """Every live member of `seed`'s album across all profiles.

        Identity rule (conservative): members sharing `seed`'s
        `mb_albumid`, or — when neither side has a conflicting `mb_albumid`
        — sharing its normalized (albumartist, album) string. Two rows with
        *different* `mb_albumid`s are never merged: a reissue is its own
        album until proven otherwise.
        """
        seed_key = (self._normalize(seed.albumartist), self._normalize(seed.album))
        members: list[_AlbumMember] = []
        for profile in _PROFILES:
            directory = self._profile_directory(profile)
            db = self._profiles_dir / f"{profile}.db"
            if not db.exists():
                continue
            try:
                with closing(sqlite3.connect(str(db))) as conn:
                    rows = conn.execute(
                        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items"
                    ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                d = dict(zip(_ITEM_COLUMNS, row))
                if profile == seed.profile and d["id"] == seed.item_id:
                    members.append(seed)
                    continue
                member = self._member_from_row(profile, directory, row)
                if member is None:
                    continue
                if seed.mb_albumid and member.mb_albumid == seed.mb_albumid:
                    members.append(member)
                    continue
                if seed.mb_albumid and member.mb_albumid != seed.mb_albumid:
                    # A different release with the same name is its own
                    # album. A seed *without* an MBID has nothing to
                    # conflict against — string-matching members join it.
                    continue
                if (
                    self._normalize(member.albumartist),
                    self._normalize(member.album),
                ) == seed_key:
                    members.append(member)
        return _AlbumGroup(members=members)

    def _group_all(self) -> list[_AlbumGroup]:
        """Every album identity in the whole library, for the repair sweep.

        Non-mbid members bucket by normalized (albumartist, album); members
        carrying `mb_albumid` bucket by it, and any string bucket matching a
        member of an mbid bucket merges into it.
        """
        members: list[_AlbumMember] = []
        for profile in _PROFILES:
            directory = self._profile_directory(profile)
            db = self._profiles_dir / f"{profile}.db"
            if not db.exists():
                continue
            try:
                with closing(sqlite3.connect(str(db))) as conn:
                    rows = conn.execute(
                        f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items"
                    ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                member = self._member_from_row(profile, directory, row)
                if member is not None:
                    members.append(member)

        mbid_groups: dict[str, list[_AlbumMember]] = {}
        str_groups: dict[tuple[str, str], list[_AlbumMember]] = {}
        for member in members:
            if member.mb_albumid:
                mbid_groups.setdefault(member.mb_albumid, []).append(member)
            else:
                key = (
                    self._normalize(member.albumartist),
                    self._normalize(member.album),
                )
                if key != ("", ""):
                    str_groups.setdefault(key, []).append(member)

        groups: list[_AlbumGroup] = []
        consumed: set[tuple[str, str]] = set()
        for mlist in mbid_groups.values():
            bucket = list(mlist)
            strings = {
                (self._normalize(m.albumartist), self._normalize(m.album))
                for m in mlist
            }
            for key, slist in str_groups.items():
                if key in strings and key not in consumed:
                    bucket.extend(slist)
                    consumed.add(key)
            groups.append(_AlbumGroup(members=bucket))
        for key, slist in str_groups.items():
            if key not in consumed:
                groups.append(_AlbumGroup(members=slist))
        return groups

    @staticmethod
    def _home_profile(members: list[_AlbumMember]) -> str:
        """Where the album converges: `library` when any member lives there
        (the canonical curated tree), else the majority tree, ties broken
        toward the earlier profile in `_PROFILES` (searches before the
        discovery trees)."""
        if any(m.profile == "library" for m in members):
            return "library"
        counts = collections.Counter(m.profile for m in members)
        return max(_PROFILES, key=lambda p: (counts[p], -_PROFILES.index(p)))

    def _canonicalize(self, group: _AlbumGroup) -> None:
        """Derive the group's canonical identity: artist, title, and — when
        the release is resolvable — track positions.

        Release resolution order: any member's `mb_albumid` (the beets
        match's release — trusted as-is); else a member's `mb_trackid`
        looked up via MusicBrainz, whose `best_release` gives the album —
        this is what makes an asis-fractured album with one pinned track
        still converge on the real title. The `mb_trackid`-derived release
        must string-match the group's majority album title to be adopted: a
        row whose MBID points at a different album must not drag the whole
        group there. When nothing resolves, the majority (albumartist,
        album) spelling stands and there is no tracklist to renumber from.
        """
        pair_counts = collections.Counter(
            (m.albumartist, m.album) for m in group.members if m.albumartist and m.album
        )
        if pair_counts:
            group.canonical_artist, group.canonical_title = pair_counts.most_common(1)[
                0
            ][0]
        else:
            group.canonical_artist = group.canonical_title = None

        mbids = {m.mb_albumid for m in group.members if m.mb_albumid}
        release_mbid = min(mbids) if mbids else None
        if release_mbid is None:
            for member in sorted(group.members, key=lambda m: m.added):
                if not member.mb_trackid:
                    continue
                try:
                    recording = self._musicbrainz.lookup_recording(member.mb_trackid)
                except Exception:
                    logger.warning(
                        "consolidation: MusicBrainz lookup failed for %s",
                        member.mb_trackid,
                        exc_info=True,
                    )
                    continue
                best = recording.best_release if recording is not None else None
                if best is not None and best.mbid:
                    if self._normalize(best.title) == self._normalize(
                        group.canonical_title
                    ):
                        release_mbid = best.mbid
                    break
        if release_mbid is None:
            return
        try:
            release = self._musicbrainz.lookup_release_tracks(release_mbid)
        except Exception:
            logger.warning(
                "consolidation: release lookup failed for %s",
                release_mbid,
                exc_info=True,
            )
            return
        if release is None or not release[0]:
            return
        group.canonical_mbid = release_mbid
        group.canonical_title = release[0]
        self._set_release_positions(group, release[1])

    def _set_release_positions(self, group: _AlbumGroup, tracks: list) -> None:
        """Build the position maps from a release's ordered track list.

        by_mbid keys every recording MBID; by_title only holds titles that
        appear exactly once on the release, so a title match is never
        ambiguous.
        """
        title_counts = collections.Counter(self._normalize(t.title) for t in tracks)
        for position, track in enumerate(tracks, start=1):
            if track.mbid:
                group.release_by_mbid[track.mbid] = position
            title_key = self._normalize(track.title)
            if title_key and title_counts[title_key] == 1:
                group.release_by_title[title_key] = position

    def _release_position(self, member: _AlbumMember, group: _AlbumGroup) -> int | None:
        """The member's track position on the canonical release: by
        recording MBID first, then by unique normalized title. None when
        the member isn't confidently on the release."""
        if not group.release_by_mbid and not group.release_by_title:
            return None
        if member.mb_trackid and member.mb_trackid in group.release_by_mbid:
            return group.release_by_mbid[member.mb_trackid]
        title_key = self._normalize(member.title)
        if title_key and title_key in group.release_by_title:
            return group.release_by_title[title_key]
        return None

    def _delete_row(self, profile: str, item_id: int) -> None:
        try:
            with closing(
                sqlite3.connect(str(self._profiles_dir / f"{profile}.db"))
            ) as conn:
                conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Could not delete library row %s from '%s'", item_id, profile
            )

    def _delete_member(self, member: _AlbumMember) -> None:
        """Delete a deduplicated member: file and library row."""
        logger.info(
            "consolidation: removing duplicate %s (%s)",
            member.path,
            member.mb_trackid or "unmatched",
        )
        member.path.unlink(missing_ok=True)
        self._delete_row(member.profile, member.item_id)

    def _dedupe_group(self, group: _AlbumGroup) -> list[_AlbumMember]:
        """Drop same-recording copies, keeping the best of each set.

        Four passes: identical `mb_trackid`; matched-vs-unmatched same
        artist+title (beets' own duplicate guard never sees these — it
        matches on mb_trackid, and the unmatched copy sailed in asis);
        unmatched-vs-unmatched same artist+title; and — when the release is
        known — same release position (live 2026-08-14: an MB-search
        re-download of "Tha Jackpot" duplicated the earlier asis "The
        Jackpot"; different titles and different MBIDs, so only the shared
        position 4 on the release gave them away). A member is only dropped
        when another member of the same claimed position actually resolves
        against the release: an album has exactly one track per position,
        so the pair is two copies of the same track. Kept: the member in
        the home tree, then the one with a recording MBID, then the
        earliest. Returns the dropped members (files already deleted).
        """
        home = group.home_profile
        dropped: list[_AlbumMember] = []

        def rank(m: _AlbumMember) -> tuple:
            return (m.profile != home, not bool(m.mb_trackid), m.added)

        def title_key(m: _AlbumMember) -> tuple[str, str, str]:
            return (
                self._normalize(m.albumartist),
                self._normalize(m.album),
                self._normalize(m.title),
            )

        by_track: dict[str, list[_AlbumMember]] = {}
        by_title: dict[tuple[str, str, str], list[_AlbumMember]] = {}
        for member in group.members:
            if member.mb_trackid:
                by_track.setdefault(member.mb_trackid, []).append(member)
            else:
                key = title_key(member)
                if key != ("", "", ""):
                    by_title.setdefault(key, []).append(member)

        for bucket in by_track.values():
            if len(bucket) < 2:
                continue
            bucket.sort(key=rank)
            dropped.extend(bucket[1:])

        # A matched member's unmatched same-title mates are copies of it —
        # the exact pair beets' own guard missed (live: See You Again twice
        # in discovery_familiar, once matched with mb_trackid, once asis).
        for member in group.members:
            if not member.mb_trackid:
                continue
            for mate in by_title.get(title_key(member), []):
                if mate not in dropped:
                    dropped.append(mate)

        for bucket in by_title.values():
            live = [m for m in bucket if m not in dropped]
            if len(live) < 2:
                continue
            live.sort(key=rank)
            dropped.extend(live[1:])

        if group.release_by_mbid or group.release_by_title:
            by_position: dict[int, list[_AlbumMember]] = {}
            release_resolved: set[int] = set()
            for member in group.members:
                position = self._release_position(member, group)
                claimed = position if position is not None else member.track
                if claimed is None or claimed <= 0:
                    continue
                by_position.setdefault(claimed, []).append(member)
                if position is not None:
                    release_resolved.add(claimed)
            for claimed, bucket in by_position.items():
                live = [m for m in bucket if m not in dropped]
                if claimed not in release_resolved or len(live) < 2:
                    continue
                live.sort(key=rank)
                dropped.extend(live[1:])

        for member in dropped:
            self._delete_member(member)
        return dropped

    def _prune_empty_dirs(self, dirs: list[Path], roots: set[Path]) -> None:
        """Remove now-empty directories up from `dirs`, stopping at (and
        never removing) the profile roots and the music root."""
        for start in dirs:
            parent = start
            while parent and parent != parent.parent:
                if parent in roots:
                    break
                try:
                    if any(parent.iterdir()):
                        break
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _consolidation_roots(self) -> set[Path]:
        roots = {self._profile_directory(p) for p in _CONSOLIDATION_ROOTS}
        roots.add(self._config.paths.music_dir)
        return roots

    def _move_member_to_profile(
        self, member: _AlbumMember, group: _AlbumGroup
    ) -> _MemberMove:
        """Re-import a member into the group's home tree.

        beets `import` with `move: yes` moves the file into the home
        profile's tree, writes tags, and creates the row there; the
        canonical album fields (and the release track position, when
        known) are forced via `--set` so the file lands directly at its
        canonical path. The stale origin row is then dropped.

        Returns the new member on success; `deduplicated=True` when beets
        refused the import because the home profile already owns this
        track — the origin copy is then deleted rather than moved. None
        (not deduplicated) when beets failed entirely, which leaves the
        origin untouched for the next sweep.
        """
        target = group.home_profile
        if target is None or member.profile == target:
            return _MemberMove(member=member)
        target_dir = self._profile_directory(target)
        target_dir.mkdir(parents=True, exist_ok=True)
        library_db = self._profiles_dir / f"{target}.db"
        cfg_path = self._write_profile_config(target, target_dir, library_db)
        before_added = self._max_added(library_db)

        set_fields = [
            f"albumartist={group.canonical_artist}",
            f"album={group.canonical_title}",
        ]
        if group.canonical_mbid is not None:
            set_fields.append(f"mb_albumid={group.canonical_mbid}")
        position = self._release_position(member, group)
        if position is not None:
            set_fields.append(f"track={position}")
        cmd = self._beet_import_command(
            cfg_path,
            member.path,
            search_id=member.mb_trackid,
            set_fields=set_fields,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._config.beets.timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "consolidation: could not re-import %s into '%s': %s",
                member.path,
                target,
                exc,
            )
            return _MemberMove()
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "beet import failed").strip()
            logger.warning(
                "consolidation: re-import of %s into '%s' failed: %s",
                member.path,
                target,
                msg,
            )
            return _MemberMove()

        item = self._latest_item(library_db, before_added, target_dir)
        if item is None:
            if member.path.exists():
                # beets skipped: the home profile already owns this track.
                logger.info(
                    "consolidation: %s is already in '%s'; deleting the origin copy",
                    member.path,
                    target,
                )
                self._delete_member(member)
                return _MemberMove(deduplicated=True)
            logger.warning(
                "consolidation: re-import of %s consumed the file but left no "
                "library row in '%s'",
                member.path,
                target,
            )
            return _MemberMove()

        new_path, _, _ = item
        if not new_path.exists():
            logger.warning(
                "consolidation: beets reported %s but nothing is there",
                new_path,
            )
            return _MemberMove()

        logger.info(
            "consolidation: moved %s from '%s' into '%s'",
            member.path,
            member.profile,
            target,
        )
        self._delete_row(member.profile, member.item_id)
        refreshed = self._read_row_by_path(target, new_path)
        if refreshed is not None:
            rebuilt = self._member_from_row(
                target, target_dir, tuple(refreshed[c] for c in _ITEM_COLUMNS)
            )
            if rebuilt is not None:
                return _MemberMove(member=rebuilt)
        return _MemberMove(member=member)

    def _read_row_by_path(self, profile: str, target: Path) -> dict | None:
        """The full items row whose resolved path equals `target`."""
        db = self._profiles_dir / f"{profile}.db"
        directory = self._profile_directory(profile)
        if not db.exists():
            return None
        try:
            with closing(sqlite3.connect(str(db))) as conn:
                rows = conn.execute(
                    f"SELECT {', '.join(_ITEM_COLUMNS)} FROM items"
                ).fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            d = dict(zip(_ITEM_COLUMNS, row))
            if self._item_path(d["path"], directory) == target:
                return d
        return None

    def _refresh_member(self, member: _AlbumMember) -> None:
        """Re-read a member's row after beets modified/wrote it, so the
        member's path and fields reflect what beets actually did (a
        renumber may have moved the file)."""
        refreshed = self._read_row_by_id(member.profile, member.item_id)
        if refreshed is None:
            return
        rebuilt = self._member_from_row(
            member.profile,
            self._profile_directory(member.profile),
            tuple(refreshed[c] for c in _ITEM_COLUMNS),
        )
        if rebuilt is not None:
            member.path = rebuilt.path
            member.albumartist = rebuilt.albumartist
            member.album = rebuilt.album
            member.track = rebuilt.track
            member.mb_albumid = rebuilt.mb_albumid

    def _file_tags_match_row(self, member: _AlbumMember) -> bool:
        """True when the file's identity tags already match the library row.

        The asis-origin tag write is idempotent, but running it on every
        sweep costs a beets subprocess and reports a spurious 'renamed'
        every time (live 2026-08-14: the Hot Vodka 2 sweep counted 3
        'renames' forever). Compare the fields that decide album grouping
        in Navidrome (albumartist/album/title/track); when they match, the
        file is already synced to the row and the write is skipped. Files
        mutagen can't parse get a write (conservative — beets knows best).
        """
        try:
            audio = mutagen.File(str(member.path), easy=True)
        except (OSError, mutagen.MutagenError):
            return False
        if audio is None:
            return False

        expected = {
            "albumartist": member.albumartist,
            "album": member.album,
            "title": member.title,
        }
        for key, value in expected.items():
            if value is None:
                continue
            got = audio.get(key)
            if isinstance(got, list):
                got = got[0] if got else None
            if got is None:
                return False
            # Exact match (case-sensitive): Navidrome groups albums by
            # exact title/artist, so a case-only difference ("Hot Vodka 2"
            # vs "HOT VODKA 2") is exactly what the write must fix.
            if str(got).strip() != str(value).strip():
                return False

        if member.track is not None:
            got = audio.get("tracknumber")
            if isinstance(got, list):
                got = got[0] if got else None
            if got is None:
                return False
            try:
                if int(str(got).split("/")[0]) != int(member.track):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _retag_member(self, member: _AlbumMember, group: _AlbumGroup) -> bool:
        """Bring one member to the group's canonical identity via
        `beet modify -m`: albumartist/album spelling, the release
        `mb_albumid` (so Navidrome groups every member of the album
        together — it keeps MBID-bearing files apart from MBID-less ones)
        and, when the release tracklist places it, its track number. The
        file moves when the path template output changes (beets handles
        tags, row and move).

        A member with no `mb_trackid` is asis-origin — beets' asis
        fallback updates the library row (the `--set` fields) but never
        writes tags to the file, so a member moved into the canonical tree
        keeps its peer's spelling and Navidrome (which groups by tags, not
        paths) still shows the old album. Such a member also gets a
        `beet write` to sync the file to its (now canonical) row — but only
        when the file's tags actually differ (`_file_tags_match_row`), so a
        converged sweep reports zero renames instead of counting the same
        idempotent write forever.

        Returns True when any beets command ran. The member's path/fields
        are refreshed from the library row afterward."""
        mods: list[str] = []
        if (
            group.canonical_artist is not None
            and member.albumartist != group.canonical_artist
        ):
            mods.append(f"albumartist={group.canonical_artist}")
        if group.canonical_title is not None and member.album != group.canonical_title:
            mods.append(f"album={group.canonical_title}")
        if (
            group.canonical_mbid is not None
            and member.mb_albumid != group.canonical_mbid
        ):
            mods.append(f"mb_albumid={group.canonical_mbid}")
        position = self._release_position(member, group)
        if position is not None and member.track != position:
            mods.append(f"track={position}")
        db = self._profiles_dir / f"{member.profile}.db"
        cfg_path = self._write_profile_config(
            member.profile, self._profile_directory(member.profile), db
        )

        ran = False
        if mods:
            cmd = [
                self._config.beets.binary,
                "--config",
                str(cfg_path),
                "modify",
                "-m",
                "--yes",
                f"path:{member.path}",
                *mods,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self._config.beets.timeout_seconds,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                logger.warning(
                    "consolidation: could not retag %s: %s", member.path, exc
                )
                return False
            if result.returncode != 0:
                msg = (result.stderr or result.stdout or "beet modify failed").strip()
                logger.warning(
                    "consolidation: retag of %s failed: %s", member.path, msg
                )
                return False
            ran = True
            self._refresh_member(member)

        if member.mb_trackid is None and not self._file_tags_match_row(member):
            # asis-origin member: the row is canonical but the file's tags
            # were never written (see the docstring). Sync them — skipped
            # when the file already matches the row (the write is
            # idempotent; running it every sweep inflated the sweep's
            # 'renamed' count forever).
            cmd = [
                self._config.beets.binary,
                "--config",
                str(cfg_path),
                "write",
                f"path:{member.path}",
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self._config.beets.timeout_seconds,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                logger.warning(
                    "consolidation: could not write tags for %s: %s",
                    member.path,
                    exc,
                )
                return ran
            if result.returncode != 0:
                msg = (result.stderr or result.stdout or "beet write failed").strip()
                logger.warning(
                    "consolidation: tag write of %s failed: %s", member.path, msg
                )
                return ran
            ran = True
            self._refresh_member(member)
        return ran

    def _unify_group(
        self, group: _AlbumGroup, seed: _AlbumMember | None = None
    ) -> tuple[dict, _AlbumMember | None]:
        """One album group, converged: dedupe, move into the home tree,
        retag/renumber every member. Returns (stats, current seed member or
        None when the seed was deduplicated away)."""
        stats = {"moved": 0, "renamed": 0, "deduplicated": 0, "errors": 0}
        group.home_profile = self._home_profile(group.members)
        self._canonicalize(group)
        if not group.canonical_title or not group.canonical_artist:
            return stats, seed
        if group.home_profile is None:
            return stats, seed

        dropped = self._dedupe_group(group)
        stats["deduplicated"] += len(dropped)
        seed_current: _AlbumMember | None = None if seed in dropped else seed
        group.members = [m for m in group.members if m not in dropped]

        prune_dirs: list[Path] = [m.path.parent for m in dropped]
        for member in list(group.members):
            if member.profile == group.home_profile:
                continue
            old_parent = member.path.parent
            move = self._move_member_to_profile(member, group)
            if move.deduplicated:
                stats["deduplicated"] += 1
                group.members.remove(member)
                if member is seed:
                    seed_current = None
                continue
            if move.member is None:
                stats["errors"] += 1
                continue
            stats["moved"] += 1
            prune_dirs.append(old_parent)
            index = group.members.index(member)
            group.members[index] = move.member
            if member is seed:
                seed_current = move.member

        for member in group.members:
            old_path = member.path
            if self._retag_member(member, group):
                stats["renamed"] += 1
                if member.path != old_path:
                    prune_dirs.append(old_path.parent)

        self._prune_empty_dirs(prune_dirs, self._consolidation_roots())
        return stats, seed_current

    def _unify_import(
        self, profile: str, before_added: float, target_path: Path
    ) -> tuple[Path, bool]:
        """Per-import hook: join the just-imported item to its album's
        canonical home. Returns (final path, deduplicated)."""
        seed = self._read_member_by_added(profile, before_added)
        if seed is None or not seed.album or not seed.albumartist:
            return target_path, False
        group = self._find_group_for(seed)
        if len(group.members) < 2 and not seed.mb_albumid and not seed.mb_trackid:
            # A lone unmatched member is its own canonical — nothing to do.
            return target_path, False
        _, seed_current = self._unify_group(group, seed)
        if seed_current is None:
            return target_path, True
        return seed_current.path, False

    def consolidate_all(self) -> dict:
        """One-shot repair sweep: group every live item by album identity
        and unify each group — the fix for albums already fractured before
        the per-import hook existed (Hot Vodka 2, Madvillainy, Flower Boy).
        Stale rows are pruned first so they cannot seed phantom groups.
        """
        summary: dict[str, int | str] = {
            "albums": 0,
            "moved": 0,
            "renamed": 0,
            "deduplicated": 0,
            "errors": 0,
        }
        if not getattr(self._config.beets, "enabled", True):
            logger.info("beets disabled — consolidation sweep skipped")
            summary["skipped"] = "beets.enabled is false"
            return summary
        for profile in _PROFILES:
            db = self._profiles_dir / f"{profile}.db"
            if db.exists():
                self._prune_missing_items(db, self._profile_directory(profile))
        for group in self._group_all():
            if (
                len(group.members) == 1
                and not group.members[0].mb_albumid
                and not group.members[0].mb_trackid
            ):
                continue
            stats, _ = self._unify_group(group)
            summary["albums"] = int(summary["albums"]) + 1
            for key in ("moved", "renamed", "deduplicated", "errors"):
                summary[key] = int(summary[key]) + stats[key]
        return summary
