-- Phase 6.8: MusicBrainz search & discovery — library downloads.
--
-- MusicBrainz-initiated downloads route into the beets "library" profile,
-- pinned to an exact recording MBID. Two new columns on `downloads`:
--   * mb_recording_id — the authoritative MusicBrainz recording MBID the
--     download was queued for (NULL for every other kind of download).
--   * is_library_download — 1 when this row came from MusicBrainz and must
--     import into the "library" profile rather than searches/discovery.
--
-- A library download is a manual-priority download: is_rec_download stays 0
-- on these rows, so has_active_manual_downloads() already gates rec queueing
-- on them without any further change.

ALTER TABLE downloads ADD COLUMN mb_recording_id TEXT;
ALTER TABLE downloads ADD COLUMN is_library_download INTEGER NOT NULL DEFAULT 0;
