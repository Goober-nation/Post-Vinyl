-- A pull song the library already had, found by name: no file record of ours.
ALTER TABLE pull_items ADD COLUMN library_id TEXT;
