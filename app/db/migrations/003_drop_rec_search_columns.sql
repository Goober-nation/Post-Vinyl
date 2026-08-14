-- searches rows are now written only for user-initiated searches (search
-- routes), never for rec-puller background searches — is_rec_search and
-- rec_track_id are therefore always 0/NULL and unused. Drop them.

ALTER TABLE searches DROP COLUMN is_rec_search;
ALTER TABLE searches DROP COLUMN rec_track_id;
