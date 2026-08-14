-- P6.6-4: track whether a beets import matched MusicBrainz metadata.
--
-- beets is configured with `quiet_fallback: asis` (app/services/beets.py) —
-- when it can't confidently match a file, it imports it anyway with its
-- existing tags rather than quarantining it or auto-accepting a low-confidence
-- guess (explicit user decision). This column is how that "imported but
-- unmatched" state surfaces to the rest of the app; NULL/0 for every row
-- moved before this migration (they went through the retired _move_file(),
-- not beets, so "unmatched" doesn't apply to them).

ALTER TABLE downloads ADD COLUMN import_unmatched BOOLEAN DEFAULT 0;
