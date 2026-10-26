-- Add columns for download lifecycle tracking
-- Wire SQLite state into the download lifecycle

ALTER TABLE downloads ADD COLUMN slskd_id TEXT;
ALTER TABLE downloads ADD COLUMN progress REAL DEFAULT 0;
ALTER TABLE downloads ADD COLUMN speed INTEGER;
ALTER TABLE downloads ADD COLUMN file_moved BOOLEAN DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_slskd_id ON downloads(slskd_id) WHERE slskd_id IS NOT NULL;
