-- #111: an attempt the user skipped ("next peer") ends as 'skipped', which
-- doesn't count toward the peer's ban. SQLite can't widen a CHECK in place,
-- so the table is rebuilt; nothing references slskd_attempts.
CREATE TABLE slskd_attempts_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id  INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    search_id    TEXT    NOT NULL REFERENCES slskd_searches(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,              -- candidate position when picked
    username     TEXT    NOT NULL,              -- peer selection skips every one listed
    filename     TEXT    NOT NULL,              -- peer's remote path
    size         INTEGER NOT NULL,
    transfer_id  TEXT,                          -- slskd's, once queued
    outcome      TEXT    CHECK (outcome IN ('completed', 'noStart', 'tooSlow', 'failed',
                                            'skipped')),
                                                -- NULL while running
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    CHECK ((outcome IS NULL) = (ended_at IS NULL))
);
INSERT INTO slskd_attempts_new SELECT * FROM slskd_attempts;
-- keep AUTOINCREMENT's counter: attempt ids name download folders, never reuse one
UPDATE sqlite_sequence
    SET seq = MAX(seq, (SELECT seq FROM sqlite_sequence WHERE name = 'slskd_attempts'))
    WHERE name = 'slskd_attempts_new';
INSERT INTO sqlite_sequence (name, seq)
    SELECT 'slskd_attempts_new', seq FROM sqlite_sequence
    WHERE name = 'slskd_attempts'
      AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'slskd_attempts_new');
DROP TABLE slskd_attempts;
ALTER TABLE slskd_attempts_new RENAME TO slskd_attempts;
CREATE INDEX idx_slskd_attempts_download ON slskd_attempts(download_id, username);
CREATE INDEX idx_slskd_attempts_search   ON slskd_attempts(search_id);
CREATE INDEX idx_slskd_attempts_transfer ON slskd_attempts(transfer_id);
