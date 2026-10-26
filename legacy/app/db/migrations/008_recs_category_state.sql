-- Phase 6.7: category-specific recommendation cursors and Deep Cuts pool.
--
-- Comfort Zone's cursor and ListenBrainz model generation must survive a
-- restart. Deep Cuts is different: ListenBrainz publishes whole playlists,
-- so musica keeps the UUIDs it has ingested and serves the resulting tracks
-- once, in order, from this local pool.

CREATE TABLE IF NOT EXISTS rec_category_state (
    category TEXT PRIMARY KEY,
    offset INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT,
    total_count INTEGER,
    warning TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deep_cuts_playlists (
    playlist_id TEXT PRIMARY KEY,
    title TEXT,
    playlist_date TEXT,
    ingested_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deep_cuts_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    artist TEXT NOT NULL,
    track TEXT NOT NULL,
    album TEXT,
    mbid TEXT,
    served_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(playlist_id, track_key),
    FOREIGN KEY (playlist_id) REFERENCES deep_cuts_playlists(playlist_id)
);

CREATE INDEX IF NOT EXISTS idx_deep_cuts_pool_unserved
    ON deep_cuts_pool(served_at, id);
