-- 2026-08-12: stop recording a duplicate-skip as a completed file move.
--
-- DownloadMonitor._import_via_beets() marked a beets duplicate-skip with
-- `mark_file_moved(transfer_id, "")` — file_moved = 1 with an empty
-- target_dir. It only ever wanted "terminal, don't re-run the import every
-- poll", but it said "this file was moved to nowhere", which is a lie the
-- rest of the app has no way to tell apart from a real move: the UI and the
-- transfer.completed SSE payload both read target_dir, /api/downloads
-- reports the row as moved, and nothing can find the file again because the
-- one field that would say where it went is blank.
--
-- Those two states are now separate. file_moved = 1 means, and only means,
-- the file is at target_dir. import_skipped = 1 means beets declined to
-- import it and the file is still wherever it was downloaded.

ALTER TABLE downloads ADD COLUMN import_skipped BOOLEAN DEFAULT 0;

-- Repair the rows the old code mislabelled. An empty/NULL target_dir with
-- file_moved = 1 is only ever produced by the duplicate path above — every
-- real move wrote str(path.parent), which cannot be empty.
--
-- These are reset to *unhandled* rather than to import_skipped = 1 on
-- purpose. Most of them were skipped because the beets library claimed a
-- track whose file no longer existed (see app/services/beets.py), so the
-- skip was wrong and the download is still sitting in
-- downloads/complete/soulseek/ waiting to be imported. Clearing the flags
-- lets DownloadMonitor make exactly one more attempt with the fixed import
-- path; if beets skips it again for a real reason, the row settles at
-- import_skipped = 1 on that poll and is never retried after that.
UPDATE downloads
   SET file_moved = 0,
       import_skipped = 0
 WHERE file_moved = 1
   AND (target_dir IS NULL OR target_dir = '');
