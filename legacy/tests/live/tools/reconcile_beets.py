#!/usr/bin/env python3
"""Reconcile the beets library DBs against what is actually on disk.

Why this exists
---------------
Each beets profile (``searches``, ``discovery``) keeps its own
``library.db``. beets' ``duplicate_action: skip`` and musica's own
``BeetsService._find_cross_profile_duplicate`` both answer "is this track
already in the library?" purely from those DBs. If the DB claims a track
that no longer exists on disk — the music tree was restructured by hand, a
volume was remounted somewhere else, files were deleted outside beets — then
every future download of that track is skipped as a duplicate and the
downloaded file is stranded in ``downloads/complete/soulseek/`` forever.
That is exactly what happened on 2026-08-12: the DBs claimed 20 + 15
imported items while the disk held one audio file in ``discovery/`` and
none in ``searches/``.

``BeetsService`` now self-heals this at import time (it prunes rows pointing
at missing files before trusting a skip), but that only fixes rows it
happens to collide with. This tool is the sweep: it reports, and optionally
removes, every dead row, and it reports audio files sitting in a managed
tree with no library row at all (files beets does not know about, which will
never be deduplicated and are invisible to any beets-driven operation).

Path translation
----------------
The DBs are written by the ``musica`` container, so their paths are
*container* paths (``/music/Discovery/...``). ``items.path`` is stored
relative to the profile's ``directory`` in beets 2.x, absolute in older
layouts; both are handled. When you run this on the host, the container's
``--music-dir`` prefix is rewritten to ``--host-music-dir`` so the existence
checks look at the real files. Run it inside the container and the two are
the same, so pass ``--host-music-dir /music`` (or nothing, if ``.env`` is
absent).

Usage
-----
    # report only (default)
    python tests/live/tools/reconcile_beets.py

    # actually delete the dead rows
    python tests/live/tools/reconcile_beets.py --apply

    # machine-readable, for the live suite
    python tests/live/tools/reconcile_beets.py --json

Exit codes: 0 = clean, 1 = drift found (dry-run) or an error occurred,
0 after ``--apply`` successfully removes the drift.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Kept in sync with app.db.download_store.ALLOWED_EXTENSIONS — duplicated
# rather than imported so this stays runnable with no PYTHONPATH set up.
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".opus"}

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROFILES_DIR = REPO_ROOT / "app_data" / "beets"
DEFAULT_MUSIC_DIR = "/music"

_DIRECTIVE_RE = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>\S.*?)\s*$")


@dataclass
class ProfileReport:
    """What reconciliation found for one beets profile."""

    profile: str
    library_db: Path
    directory: str
    host_directory: Path
    total_items: int = 0
    missing: list[dict] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    removed_items: int = 0
    removed_albums: int = 0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return not self.error and not self.missing and not self.untracked

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "library_db": str(self.library_db),
            "directory": self.directory,
            "host_directory": str(self.host_directory),
            "total_items": self.total_items,
            "missing_count": len(self.missing),
            "missing": self.missing,
            "untracked_count": len(self.untracked),
            "untracked": self.untracked,
            "removed_items": self.removed_items,
            "removed_albums": self.removed_albums,
            "error": self.error,
        }


def read_profile_directive(yaml_path: Path, key: str) -> str | None:
    """Pull a single top-level scalar out of a beets profile config.

    Deliberately not a YAML parse: these files are generated from one
    template in ``BeetsService._write_profile_config`` and PyYAML is not a
    declared dependency of this repo (it only arrives transitively with
    beets, which need not be installed on the host running this tool).
    """
    if not yaml_path.is_file():
        return None
    for line in yaml_path.read_text().splitlines():
        match = _DIRECTIVE_RE.match(line)
        if match and match.group("key") == key:
            return match.group("value")
    return None


def decode_path(raw: object) -> str:
    """beets stores ``items.path`` as a BLOB; sqlite hands it back as bytes."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "surrogateescape")
    return str(raw)


def to_host_path(container_path: str, music_dir: str, host_music_dir: Path) -> Path:
    """Rewrite a container-visible path to where it lives on this machine."""
    music_dir = music_dir.rstrip("/")
    if not music_dir or str(host_music_dir) == music_dir:
        return Path(container_path)
    if container_path == music_dir:
        return host_music_dir
    if container_path.startswith(music_dir + "/"):
        return host_music_dir / container_path[len(music_dir) + 1 :]
    return Path(container_path)


def discover_profiles(profiles_dir: Path) -> list[str]:
    """Every profile with a library DB, ordered for stable output."""
    return sorted(p.stem for p in profiles_dir.glob("*.db") if p.is_file())


def scan_audio_files(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {
        p.resolve()
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    }


def reconcile_profile(
    profile: str,
    profiles_dir: Path,
    music_dir: str,
    host_music_dir: Path,
    apply: bool,
) -> ProfileReport:
    library_db = profiles_dir / f"{profile}.db"
    directory = (
        read_profile_directive(profiles_dir / f"{profile}.yaml", "directory")
        # Fall back to the layout BeetsService uses when the profile config
        # has not been written yet (it is rewritten on every import).
        or f"{music_dir.rstrip('/')}/{profile.capitalize()}"
    )
    host_directory = to_host_path(directory, music_dir, host_music_dir)
    report = ProfileReport(
        profile=profile,
        library_db=library_db,
        directory=directory,
        host_directory=host_directory,
    )

    if not library_db.is_file():
        report.error = f"library db not found: {library_db}"
        return report

    try:
        conn = sqlite3.connect(str(library_db))
    except sqlite3.Error as exc:
        report.error = f"cannot open {library_db}: {exc}"
        return report

    live_paths: set[Path] = set()
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, path, artist, title, album, mb_trackid FROM items"
            ).fetchall()
            report.total_items = len(rows)

            for item_id, raw_path, artist, title, album, mb_trackid in rows:
                stored = decode_path(raw_path)
                container_path = (
                    stored
                    if stored.startswith("/")
                    else f"{directory.rstrip('/')}/{stored}"
                )
                host_path = to_host_path(container_path, music_dir, host_music_dir)
                if host_path.is_file():
                    live_paths.add(host_path.resolve())
                    continue
                report.missing.append(
                    {
                        "id": item_id,
                        "path": container_path,
                        "host_path": str(host_path),
                        "artist": artist,
                        "title": title,
                        "album": album,
                        "mb_trackid": mb_trackid,
                    }
                )

            if apply and report.missing:
                ids = [m["id"] for m in report.missing]
                placeholders = ",".join("?" * len(ids))
                cursor = conn.execute(
                    f"DELETE FROM items WHERE id IN ({placeholders})", ids
                )
                report.removed_items = cursor.rowcount
                # An album row whose every item just went away is dead
                # weight: beets would keep reporting it, and its `artpath`
                # points into a directory that no longer exists.
                cursor = conn.execute(
                    "DELETE FROM albums WHERE id NOT IN "
                    "(SELECT album_id FROM items WHERE album_id IS NOT NULL)"
                )
                report.removed_albums = cursor.rowcount
    except sqlite3.Error as exc:
        report.error = f"sqlite error on {library_db}: {exc}"
        return report
    finally:
        conn.close()

    on_disk = scan_audio_files(host_directory)
    report.untracked = sorted(str(p) for p in on_disk - live_paths)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile beets library DBs against the files on disk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help=f"directory holding <profile>.db / <profile>.yaml (default: {DEFAULT_PROFILES_DIR})",
    )
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        help="profile to check; repeatable. Default: every *.db in --profiles-dir",
    )
    parser.add_argument(
        "--music-dir",
        default=DEFAULT_MUSIC_DIR,
        help=f"music root as the container sees it (default: {DEFAULT_MUSIC_DIR})",
    )
    parser.add_argument(
        "--host-music-dir",
        type=Path,
        default=None,
        help="music root on this machine (default: MUSIC_HOST_DIR from .env, else --music-dir)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="report only; the default",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="delete library rows whose file is missing (never touches files)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def host_music_dir_from_env(repo_root: Path) -> Path | None:
    """Read MUSIC_HOST_DIR out of .env without depending on python-dotenv."""
    env_file = repo_root / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("MUSIC_HOST_DIR="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            if value:
                return Path(value)
    return None


def render_text(reports: list[ProfileReport], apply: bool) -> str:
    out: list[str] = []
    for r in reports:
        out.append(f"── profile: {r.profile}")
        out.append(f"   library : {r.library_db}")
        out.append(f"   tree    : {r.directory}  ->  {r.host_directory}")
        if r.error:
            out.append(f"   ERROR   : {r.error}")
            out.append("")
            continue
        out.append(f"   items   : {r.total_items} rows, {len(r.missing)} pointing at missing files")
        for m in r.missing:
            out.append(f"     [missing] {m['artist']} - {m['title']}  ({m['host_path']})")
        out.append(f"   on disk : {len(r.untracked)} audio file(s) with no library row")
        for u in r.untracked:
            out.append(f"     [untracked] {u}")
        if apply:
            out.append(
                f"   applied : removed {r.removed_items} item row(s), "
                f"{r.removed_albums} orphaned album row(s)"
            )
        out.append("")

    total_missing = sum(len(r.missing) for r in reports)
    total_untracked = sum(len(r.untracked) for r in reports)
    errors = [r for r in reports if r.error]
    if errors:
        out.append(f"{len(errors)} profile(s) could not be read.")
    if total_missing == 0 and total_untracked == 0 and not errors:
        out.append("Clean: every library row points at a real file and every file is tracked.")
    elif not apply:
        out.append(
            f"Drift: {total_missing} dead row(s), {total_untracked} untracked file(s). "
            "Re-run with --apply to remove the dead rows "
            "(untracked files are only reported — importing them is beets' job)."
        )
    else:
        out.append(
            f"Removed {sum(r.removed_items for r in reports)} dead row(s). "
            f"{total_untracked} untracked file(s) remain — reported only, never deleted."
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    apply = bool(args.apply)

    profiles_dir: Path = args.profiles_dir
    if not profiles_dir.is_dir():
        print(f"error: --profiles-dir does not exist: {profiles_dir}", file=sys.stderr)
        return 1

    host_music_dir = (
        args.host_music_dir
        or host_music_dir_from_env(REPO_ROOT)
        or Path(args.music_dir)
    )

    profiles = args.profiles or discover_profiles(profiles_dir)
    if not profiles:
        print(f"error: no *.db profiles found in {profiles_dir}", file=sys.stderr)
        return 1

    reports = [
        reconcile_profile(p, profiles_dir, args.music_dir, host_music_dir, apply)
        for p in profiles
    ]

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        print(render_text(reports, apply))

    if any(r.error for r in reports):
        return 1
    if apply:
        return 0
    return 0 if all(r.clean for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
