-- Initial database schema
-- Creates all tables for Musica

-- Search history and metadata
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    artist TEXT,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    response_count INTEGER DEFAULT 0,
    file_count INTEGER DEFAULT 0,
    is_rec_search BOOLEAN DEFAULT 0,
    rec_track_id TEXT
);

-- Download tracking
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    search_id TEXT,
    username TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER,
    state TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    is_rec_download BOOLEAN DEFAULT 0,
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    target_dir TEXT,
    FOREIGN KEY (search_id) REFERENCES searches(id)
);

-- Peer reputation
CREATE TABLE IF NOT EXISTS peers (
    username TEXT PRIMARY KEY,
    failure_count INTEGER DEFAULT 0,
    is_blocked BOOLEAN DEFAULT 0,
    last_seen INTEGER,
    blocked_at INTEGER
);

-- Retry tracking (per download)
CREATE TABLE IF NOT EXISTS download_retry_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id TEXT NOT NULL,
    username TEXT NOT NULL,
    attempted_at INTEGER NOT NULL,
    success BOOLEAN DEFAULT 0,
    FOREIGN KEY (download_id) REFERENCES downloads(id)
);

-- Recommendation tracking
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    artist TEXT,
    track TEXT,
    mbid TEXT,
    status TEXT NOT NULL,
    search_id TEXT,
    download_id TEXT,
    playlist_id TEXT,
    created_at INTEGER NOT NULL,
    processed_at INTEGER,
    FOREIGN KEY (search_id) REFERENCES searches(id),
    FOREIGN KEY (download_id) REFERENCES downloads(id)
);

-- Love/hate sync state
CREATE TABLE IF NOT EXISTS sync_state (
    song_id TEXT PRIMARY KEY,
    song_type TEXT NOT NULL,
    mbid TEXT,
    synced_at INTEGER NOT NULL,
    lb_synced BOOLEAN DEFAULT 0
);

-- Configuration (persisted settings)
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- User sessions (for multi-user, deferred)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_login INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_downloads_state ON downloads(state);
CREATE INDEX IF NOT EXISTS idx_downloads_search_id ON downloads(search_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations(status);
CREATE INDEX IF NOT EXISTS idx_recommendations_mbid ON recommendations(mbid);
CREATE INDEX IF NOT EXISTS idx_sync_state_song_type ON sync_state(song_type);
CREATE INDEX IF NOT EXISTS idx_sync_state_lb_synced ON sync_state(lb_synced);
