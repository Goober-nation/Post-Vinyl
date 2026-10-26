-- Tracks Navidrome playlist IDs by stable role (trash, comfort_zone,
-- fresh_picks, deep_cuts), so a rename performed inside Navidrome doesn't
-- orphan musica's bookkeeping the way find-by-name alone does.
CREATE TABLE IF NOT EXISTS playlist_registry (
    role TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
