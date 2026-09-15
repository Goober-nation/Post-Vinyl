-- #19: track whether a completed manual (non-rec) download has been added
-- to the "Searches" playlist yet — recs already have this via
-- recommendations.playlist_id, manual downloads had no equivalent column at
-- all, which is one reason no "Searches" playlist role ever got created.
--
-- NULL means "not yet linked" (or not eligible — a rec download never gets
-- this column touched, it uses its own recommendations.playlist_id).

ALTER TABLE downloads ADD COLUMN playlist_id TEXT;
