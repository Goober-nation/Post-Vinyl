"""
The destructive full reset, and the backup that has to survive it.

The user explicitly authorised wiping the live state so a run starts from a
known-empty pipeline. That is only defensible with a backup taken first, so
this module does them in one function, in one order, and **refuses to wipe
anything it did not just copy**. If the backup step fails, nothing is
deleted.

What gets reset, and why each one matters:

- `app_data/musica.db` — musica's own bookkeeping. Migrations recreate it on
  the next start, so wiping it is how a run gets an empty `downloads` and
  `searches` table without hand-editing rows.
- `app_data/beets/{searches,discovery}.db` — the beets libraries. **These
  are the ones that actually matter.** On the live stack right now they
  hold 15 and 21 rows respectively while the music tree holds *one* audio
  file: every one of those rows is a stale "already in the library" that
  will silently skip the next download of that track. A reset that cleared
  the tree but kept these would reproduce the exact bug it is trying to
  measure.
- The `Searches`, `Discovery` and `downloads` subtrees — the files
  themselves.

musica is stopped *before* the wipe and started after, rather than
restarted at the end: deleting a SQLite file out from under a running
process leaves it writing happily to an unlinked inode, and the state that
comes back is neither the old one nor a clean one.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from tests.live.harness import DbInspector, DockerControl, MusicaClient, SlskdClient
from tests.live.probes.beets_lib import PROFILES
from tests.live.probes.paths import (
    beets_profiles_dir,
    music_host_root,
    musica_db_path,
    tree_path,
)

#: Trees emptied by a full reset. `library` is deliberately absent — it is
#: the user's own music and slskd's shared folder, not pipeline output.
RESET_TREES: tuple[str, ...] = ("searches", "discovery", "downloads")


@dataclass
class ResetReport:
    """What a reset actually did — recorded so a run can prove it."""

    backup_dir: Path
    backed_up: list[Path] = field(default_factory=list)
    wiped: list[Path] = field(default_factory=list)
    downtime_s: float = 0.0
    tables: set[str] = field(default_factory=set)
    slskd_transfers_forgotten: int = 0
    slskd_transfers_forget_failed: int = 0


def _backup_file(source: Path, dest_root: Path, relative: Path) -> Path | None:
    if not source.exists():
        return None
    dest = dest_root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    if not dest.exists():
        raise RuntimeError(f"backup of {source} to {dest} produced nothing")
    return dest


def _backup_tree(source: Path, dest_root: Path, relative: Path) -> Path | None:
    if not source.is_dir():
        return None
    dest = dest_root / relative
    shutil.copytree(source, dest, dirs_exist_ok=True, symlinks=True)
    if not dest.exists():
        raise RuntimeError(f"backup of {source} to {dest} produced nothing")
    return dest


def _empty_dir(directory: Path) -> list[Path]:
    """Delete a directory's *contents*, never the directory.

    The tree roots are bind-mount sources; removing one would leave the
    containers pointed at a path that no longer exists, and the stack would
    need recreating rather than restarting.
    """
    removed: list[Path] = []
    if not directory.is_dir():
        return removed
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(entry)
    return removed


def _unlink_db(path: Path) -> list[Path]:
    """Remove a SQLite file and its write-ahead log siblings."""
    removed = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate)
    return removed


def full_reset(
    backup_dir: Path,
    *,
    musica_url: str,
    db_path: Path | None = None,
    restart: bool = True,
    startup_timeout: float = 180.0,
) -> ResetReport:
    """Back up, then wipe, then bring musica back on a clean database.

    Raises before deleting anything if the backup could not be written.
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    report = ResetReport(backup_dir=backup_dir)

    music_root = music_host_root()
    if not music_root.is_dir():
        raise RuntimeError(f"MUSIC_HOST_DIR does not exist: {music_root}")

    musica_db = Path(db_path) if db_path else musica_db_path()
    profiles_dir = beets_profiles_dir()
    beets_dbs = [profiles_dir / f"{p}.db" for p in PROFILES]
    trees = []
    for kind in RESET_TREES:
        tree = tree_path(kind)
        # Never let a misconfigured path turn this into "delete the music
        # library". The tree must be a real child of the music root.
        if tree == music_root or music_root not in tree.parents:
            raise RuntimeError(f"refusing to wipe {tree}: not inside {music_root}")
        trees.append(tree)

    # -- 1. back up, and prove it landed ----------------------------------
    for source, relative in (
        (musica_db, Path("app_data/musica.db")),
        *((db, Path("app_data/beets") / db.name) for db in beets_dbs),
    ):
        saved = _backup_file(source, backup_dir, relative)
        if saved is not None:
            report.backed_up.append(saved)
    for tree in trees:
        saved = _backup_tree(tree, backup_dir, Path("music") / tree.name)
        if saved is not None:
            report.backed_up.append(saved)

    # -- 2. stop musica so nothing writes during the wipe -----------------
    docker = DockerControl()
    client = MusicaClient(musica_url)
    start = time.monotonic()
    if restart:
        docker.stop("musica")

    # -- 3. wipe -----------------------------------------------------------
    report.wiped.extend(_unlink_db(musica_db))
    for db in beets_dbs:
        report.wiped.extend(_unlink_db(db))
    for tree in trees:
        report.wiped.extend(_empty_dir(tree))

    # slskd's own config (`slskd_config/slskd.yml`, `directories.downloads`)
    # points at downloads/complete/soulseek and validates it exists at
    # *startup* — but slskd is never restarted by this reset (only musica
    # is), so a wipe that empties it goes unnoticed until the next time
    # slskd itself restarts for any reason, at which point it refuses to
    # start at all. Recreate it every time so a reset can never leave the
    # stack in a state where restarting slskd is broken. (Live-confirmed
    # 2026-08-12: exactly this happened — a manual slskd restart hours
    # after a reset crash-looped on "non-existent directory".)
    downloads_tree = tree_path("downloads")
    if downloads_tree in trees:
        (downloads_tree / "complete" / "soulseek").mkdir(parents=True, exist_ok=True)

    # slskd is a separate container with its own transfer history, untouched
    # by wiping musica.db above — it keeps reporting every transfer from
    # every previous run as "Completed, Succeeded" forever. musica, now
    # empty, adopts every one of those on its first poll as a brand-new
    # "completed" download (upsert_transfer has no way to tell "ancient
    # history" from "just finished"), pointing at a file this reset just
    # deleted. That file can never be found, so DownloadMonitor cannot ever
    # mark it handled — see the import_handled discussion this was written
    # for. Forgetting slskd's own transfers here is what stops a reset from
    # manufacturing that backlog on every single run.
    forgotten = 0
    forget_failed = 0
    try:
        slskd = SlskdClient()
        for entry in slskd.downloads():
            username = entry.get("username")
            transfer_id = entry.get("id")
            if not username or not transfer_id:
                continue
            if slskd.forget_transfer(username, transfer_id):
                forgotten += 1
            else:
                forget_failed += 1
    except Exception:
        # Best-effort: slskd being unreachable here must not block the
        # reset musica actually needs to come back clean.
        forget_failed += 1
    report.slskd_transfers_forgotten = forgotten
    report.slskd_transfers_forget_failed = forget_failed

    # -- 4. bring it back and check it came back clean ---------------------
    if restart:
        docker.start("musica")
        client.wait_until_up(timeout=startup_timeout)
        report.downtime_s = round(time.monotonic() - start, 2)

        deadline = time.monotonic() + 60
        inspector = DbInspector(musica_db)
        while time.monotonic() < deadline:
            if musica_db.exists() and not inspector.missing_tables():
                break
            time.sleep(1.0)
        if not musica_db.exists():
            raise RuntimeError(
                f"musica restarted but did not recreate {musica_db} — migrations "
                f"did not run; the backup is in {backup_dir}"
            )
        missing = inspector.missing_tables()
        if missing:
            raise RuntimeError(
                f"musica recreated {musica_db} but {sorted(missing)} are missing — "
                f"migrations did not complete; the backup is in {backup_dir}"
            )
        report.tables = inspector.tables()

    return report
