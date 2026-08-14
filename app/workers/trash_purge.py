"""
TrashPurge — P6.7-6 (formerly P5-4) background worker.

Every `sync.interval_hours` (default 12h), and once at startup:

1. Fetch the Navidrome Trash playlist and dispose of each entry: send -1
   feedback to ListenBrainz (retried until delivered), delete the file from
   disk (real path via Navidrome's native API — the Subsonic `path` field is
   tag-synthesized and cannot locate a file), then remove the entry from the
   Trash playlist.
2. Sweep stranded downloads: `downloads WHERE import_skipped = 1 AND
   file_moved = 0` — files beets declined (duplicate skip, or a completed
   transfer whose source never appeared on disk) that sit in
   `downloads/complete/soulseek/` with nothing else disposing of them. The
   file is deleted and its now-empty parent directories are pruned.
3. Trigger a Navidrome scan whenever files were deleted, so the library
   (and any playlist entry pointing at the purged file) catches up.

This is musica's responsibility, not beets' (explicit user decision
2026-08-10): Trash means "I hate this" — the file goes away.
"""

import threading
from pathlib import Path

from app.db.database import Database
from app.db.download_store import DownloadStore
from app.db.sync_store import HATE, SyncStore
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzDisabledError,
    ListenBrainzFeedbackError,
)
from app.logging_config import get_logger

logger = get_logger(__name__)

TRASH_PLAYLIST_NAME = "Trash"


class TrashPurge:
    """
    Processes the Navidrome Trash playlist and stranded downloads.

    Runs as a daemon thread. purge_once() is synchronous and independently
    testable; start() / stop() manage the thread.
    """

    def __init__(
        self,
        config,
        library_service,
        feedback_service,
        database: Database,
        sync_store: SyncStore | None = None,
        download_store: DownloadStore | None = None,
    ) -> None:
        self._config = config
        self._library = library_service
        self._feedback = feedback_service
        self._sync_store = sync_store or SyncStore(database)
        self._download_store = download_store or DownloadStore(database)
        self._stopped = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background worker thread."""
        if self._thread is not None:
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        logger.info("TrashPurge started (interval=%dh)", self._interval_hours())

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self.request_stop()
        self.join(timeout=5)

    def request_stop(self) -> None:
        """Signal the thread to stop without waiting (non-blocking)."""
        self._stopped.set()

    def join(self, timeout: float | None = 5) -> None:
        """Wait for the thread to finish (call request_stop() first)."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("TrashPurge stopped")

    def run(self) -> None:
        """Main loop — purges once at startup, then every interval.

        A startup run is intentional (user decision 2026-08-13): the purge
        is idempotent (sync_state guards feedback, deleting an already-gone
        file is a no-op) and a 12h first-run wait is long.
        """
        while not self._stopped.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Unhandled error in TrashPurge.purge_once")
            self._stopped.wait(self._interval_hours() * 3600)

    def run_once(self) -> dict:
        """Run one pass without overlapping a periodic or manual pass."""
        with self._run_lock:
            return self.purge_once()

    def purge_once(self) -> dict:
        """One purge cycle (sync, independently testable).

        Returns a summary dict.
        """
        files_deleted: list[Path] = []

        # 1. Trash playlist
        trash_id = self._find_trash_playlist()
        trashed = 0
        feedback_pending = 0
        if trash_id is not None:
            try:
                detail = self._library.get_playlist_detail(trash_id)
            except Exception:  # noqa: BLE001 — playlist backends vary
                logger.warning("TrashPurge: get_playlist_detail failed for Trash")
                detail = None
            if detail is not None:
                for entry in detail.songs:
                    # Feedback is retried every cycle until delivered, but it
                    # never blocks file disposal — a trashed file goes away
                    # even when ListenBrainz is down or disabled.
                    pending = self._sync_store.needs_feedback(
                        entry.song_id, HATE
                    ) and not self._send_hate(entry)
                    if pending:
                        feedback_pending += 1

                    deleted = self._delete_file(entry.song_id)
                    if deleted is None:
                        # Path unresolved — leave the entry in Trash so the
                        # deletion is retried next cycle; removing it now
                        # would strand the file in the library forever.
                        continue
                    files_deleted.append(deleted)
                    if not pending:
                        # Only leave Trash once the -1 is settled (or had
                        # nothing to send) — a pending entry stays for the
                        # retry, and the file is already gone.
                        self._remove_from_trash(trash_id, entry.song_id)
                        trashed += 1

        # 2. Stranded downloads sweep
        stranded = self._sweep_stranded_downloads()
        files_deleted.extend(stranded)

        # 3. Scan once if anything disappeared from disk
        scan_triggered = False
        if files_deleted:
            try:
                self._library.trigger_scan()
                scan_triggered = True
            except Exception:
                logger.warning("TrashPurge: trigger_scan failed", exc_info=True)

        logger.info(
            "TrashPurge: %d trashed, %d feedback pending, %d file(s) deleted, "
            "scan_triggered=%s",
            trashed,
            feedback_pending,
            len(files_deleted),
            scan_triggered,
        )
        return {
            "trashed": trashed,
            "feedback_pending": feedback_pending,
            "files_deleted": [str(p) for p in files_deleted],
            "scan_triggered": scan_triggered,
        }

    # ------------------------------------------------------------------
    # Trash playlist
    # ------------------------------------------------------------------

    def _find_trash_playlist(self) -> str | None:
        """Locate the Trash playlist by name, without creating it.

        Creation is the puller's lazy job (a playlist deleted by the user
        stays deleted until there is a track to add to it); a missing Trash
        simply means there is nothing to sweep.
        """
        try:
            existing = self._library.list_playlists()
        except Exception:  # noqa: BLE001 — library backends vary
            logger.warning("TrashPurge: list_playlists failed")
            return None
        match = next(
            (p for p in existing if p.name.lower() == TRASH_PLAYLIST_NAME.lower()),
            None,
        )
        return match.playlist_id if match is not None else None

    def _send_hate(self, entry) -> bool:
        """Send -1 for one trashed entry and record the sync_state row.

        Returns True when feedback is settled; False when it remains
        outstanding (ListenBrainz unreachable or disabled) and the entry
        must stay in the Trash playlist for the next cycle.
        """
        if not entry.mbid:
            self._sync_store.record(entry.song_id, HATE, None, lb_synced=1)
            return True
        try:
            ok = self._feedback.send_feedback(entry.mbid, -1)
        except ListenBrainzDisabledError:
            self._sync_store.record(entry.song_id, HATE, entry.mbid, lb_synced=0)
            return False
        except (ListenBrainzConnectionError, ListenBrainzFeedbackError) as e:
            logger.error(
                "TrashPurge: feedback failed for %s (%s): %s",
                entry.song_id,
                entry.title,
                e,
            )
            self._sync_store.record(entry.song_id, HATE, entry.mbid, lb_synced=0)
            return False
        except Exception as e:  # noqa: BLE001 — feedback impls vary
            logger.error(
                "TrashPurge: unexpected feedback error for %s: %s", entry.song_id, e
            )
            self._sync_store.record(entry.song_id, HATE, entry.mbid, lb_synced=0)
            return False

        if ok:
            self._sync_store.record(entry.song_id, HATE, entry.mbid, lb_synced=1)
            logger.info("TrashPurge: -1 for %s - %s", entry.artist, entry.title)
            return True
        self._sync_store.record(entry.song_id, HATE, entry.mbid, lb_synced=0)
        return False

    def _delete_file(self, song_id: str) -> Path | None:
        """Delete a trashed song's file from disk.

        Resolves the real path via Navidrome's native API (Subsonic's
        `path` is tag-synthesized and unusable for file operations).
        Returns the path when the file is confirmed gone from disk (deleted
        now, or already gone — e.g. a previous cycle's deletion whose Trash
        removal failed); None when it could not be resolved at all, which
        leaves the entry in the Trash playlist for the next cycle.
        """
        try:
            real_path = self._library.get_song_real_path(song_id)
        except Exception as e:  # noqa: BLE001 — library backends vary
            logger.error("TrashPurge: real-path lookup failed for %s: %s", song_id, e)
            return None
        if real_path is None:
            logger.warning("TrashPurge: no real path for song %s", song_id)
            return None

        full = Path(real_path)
        if not full.is_absolute():
            full = Path(self._config.paths.music_dir) / real_path
        if not full.is_file():
            logger.warning("TrashPurge: %s already gone from disk", full)
            return full
        try:
            full.unlink()
        except OSError as e:
            logger.error("TrashPurge: failed to delete %s: %s", full, e)
            return None
        logger.info("TrashPurge: deleted %s", full)
        self._prune_empty_parents(full)
        return full

    def _remove_from_trash(self, trash_id: str, song_id: str) -> None:
        """Remove one entry from the Trash playlist (best-effort)."""
        try:
            self._library.remove_songs_from_playlist(trash_id, [song_id])
        except Exception as e:  # noqa: BLE001 — playlist backends vary
            logger.warning("TrashPurge: failed to remove %s from Trash: %s", song_id, e)

    # ------------------------------------------------------------------
    # Stranded downloads (extended scope, 2026-08-12)
    # ------------------------------------------------------------------

    def _sweep_stranded_downloads(self) -> list[Path]:
        """Delete files of declined imports and prune empty directories.

        A stranded download is exactly `downloads WHERE import_skipped = 1
        AND file_moved = 0`: beets skipped a duplicate or gave up on a
        missing source, and nothing else ever disposes of the file. Rows
        still inside DownloadMonitor's missing-source timeout window are
        not `import_skipped` yet, so they are never selected here.
        """
        removed: list[Path] = []
        try:
            rows = self._download_store.get_stranded_downloads()
        except Exception:
            logger.warning("TrashPurge: stranded-download query failed", exc_info=True)
            return removed

        for row in rows:
            source = self._resolve_stranded_source(row)
            if source is None or not source.exists():
                continue
            try:
                source.unlink()
            except OSError as e:
                logger.error("TrashPurge: failed to delete stranded %s: %s", source, e)
                continue
            logger.info("TrashPurge: deleted stranded download %s", source)
            removed.append(source)
            self._prune_empty_parents(source)
        return removed

    def _resolve_stranded_source(self, row: dict) -> Path | None:
        """Locate a stranded download's file on disk.

        Mirrors DownloadMonitor._resolve_source_path: exact reported path
        first, basename search as a fallback.
        """
        username = row.get("username") or ""
        filename = (row.get("filename") or "").replace("\\", "/")
        basename = filename.rsplit("/", 1)[-1]

        source_dir = (
            self._config.paths.download_path / "complete" / "soulseek" / username
        )
        if not source_dir.exists():
            return None

        direct = source_dir / filename
        if direct.is_file():
            return direct
        for candidate in source_dir.glob("**/*"):
            if candidate.is_file() and candidate.name == basename:
                return candidate
        return None

    def _prune_empty_parents(self, file: Path) -> None:
        """Remove now-empty directories up from `file`'s parent, stopping at
        (and never touching) the downloads root."""
        root = self._config.paths.download_path
        parent = file.parent
        while parent != root and parent.is_dir() and parent != parent.parent:
            try:
                if any(parent.iterdir()):
                    return
                parent.rmdir()
                logger.debug("TrashPurge: pruned empty dir %s", parent)
                parent = parent.parent
            except OSError:
                return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _interval_hours(self) -> int:
        """Read the (hot-reloadable) sync interval."""
        return max(
            1, int(getattr(getattr(self._config, "sync", None), "interval_hours", 12))
        )
