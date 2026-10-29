-- Post-Vinyl initial schema (#84). Design: agents_memory/rebuild/core-db-redesign.md.
-- Times are unix seconds (INTEGER). Every FK column is indexed (SQLite doesn't do it).
-- States are CHECKed here; who may change them is core/states.py (milestone 5).

-- ---------------------------------------------------------------- users & auth

-- every Post-Vinyl user is a Navidrome user; row created on their first login
CREATE TABLE users (
    id               INTEGER PRIMARY KEY,
    username         TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- = Navidrome username
    nd_password_enc  TEXT    NOT NULL,          -- encrypted (PV_SECRET_KEY); refreshed each login
    lb_token_enc     TEXT,                      -- encrypted; NULL until the user enters it
    is_admin         INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),  -- from Navidrome
    status           TEXT    NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'needs_reauth')),
    created_at       INTEGER NOT NULL           -- failed logins / lockout: in memory (auth service)
);

-- login sessions, 30-day sliding expiry
CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT    NOT NULL UNIQUE,      -- SHA-256 of the cookie token; raw never stored
    created_at    INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL              -- pushed forward on each use
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

-- loose per-user preferences; anything needing encryption or constraints gets a real column
CREATE TABLE user_settings (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,               -- JSON
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- ---------------------------------------------------------------- songs

-- every song asked for through Post-Vinyl; metadata only, shared by all users
CREATE TABLE songs (
    id            INTEGER PRIMARY KEY,
    title         TEXT    NOT NULL,
    artist_name   TEXT    NOT NULL,             -- full credit as the catalog writes it
    -- core/normalize output from the credit parts, for the slskd ladder and matcher
    -- (#101); NULL for a song with no catalog credit (falls back to title/artist_name)
    query_title   TEXT,
    main_artist   TEXT,
    album_title   TEXT,
    track_number  INTEGER,
    disc_number   INTEGER CHECK (disc_number >= 1),   -- NULL = no disc
    disc_count    INTEGER,                            -- disc folder only if > 1
    duration_s    INTEGER
);

-- source ids: 'musicbrainz' (recording MBID), 'deezer', 'isrc' (a hint, may repeat), …
CREATE TABLE song_identity (
    song_id    INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    source     TEXT    NOT NULL,
    source_id  TEXT    NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX idx_song_identity_song ON song_identity(song_id);

-- ---------------------------------------------------------------- requests

-- where a request came from (optional); type tables reuse the origins id
CREATE TABLE origins (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL                         -- catalog_search | yandex | …
);

-- typed catalog-search history, per user; a repeat bumps last_used_at
CREATE TABLE catalog_searches (
    id            INTEGER PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query         TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('track', 'album', 'artist')),
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_catalog_searches_query
    ON catalog_searches(user_id, lower(query), kind);

-- one row per track asked for, whatever started it; never pruned
CREATE TABLE requests (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    song_id       INTEGER REFERENCES songs(id),   -- typed Soulseek search: set when a file is
                                                --   queued (matched, or a song from the file)
    source        TEXT    NOT NULL,             -- slskd | catalog | rec | yandex | …
    origin_id     INTEGER REFERENCES origins(id) ON DELETE SET NULL,
    query         TEXT,                         -- typed query, or "artist – title"
    track_state   TEXT    NOT NULL DEFAULT 'requested' CHECK (track_state IN (
                      'requested', 'dropped', 'reRequested', 'searching', 'found',
                      'notFound', 'midDownload', 'downloaded', 'notDownloaded', 'badDownload')),
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL              -- bumped when a typed Soulseek query repeats
);
CREATE INDEX idx_requests_user      ON requests(user_id, last_used_at);  -- paginated history
CREATE INDEX idx_requests_song      ON requests(song_id);
CREATE INDEX idx_requests_origin    ON requests(origin_id);
CREATE UNIQUE INDEX idx_requests_slskd_query
    ON requests(user_id, lower(query)) WHERE source = 'slskd';

-- ---------------------------------------------------------------- downloads (generic)

-- one row per file wanted, whatever source fetches it; kept through every retry
CREATE TABLE downloads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- ours, never changes
    request_id     INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    source         TEXT    NOT NULL,            -- slskd | …  (which backend owns this row)
    state          TEXT    NOT NULL DEFAULT 'queued' CHECK (state IN (
                       'queued', 'downloading', 'completed', 'failed', 'cancelled')),
    file_location  TEXT,                        -- finished file, relative to the source's
                                                --   download dir; set on completion
    created_at     INTEGER NOT NULL,            -- start of the retry chain
    completed_at   INTEGER,
    CHECK (state <> 'completed' OR file_location IS NOT NULL)
);
CREATE INDEX idx_downloads_request ON downloads(request_id);
CREATE INDEX idx_downloads_state   ON downloads(state);

-- ---------------------------------------------------------------- slskd backend

-- one row per real slskd search (a strategy = several rows per request)
CREATE TABLE slskd_searches (
    id          TEXT    PRIMARY KEY,            -- slskd's search id (re-readable later)
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    query       TEXT    NOT NULL,               -- exact text sent to slskd
    strategy    TEXT    NOT NULL,               -- L0 | TA | T | album | …
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_slskd_searches_request ON slskd_searches(request_id);

-- every peer/file tried for a download; retries = COUNT(*) - 1
CREATE TABLE slskd_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id  INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    search_id    TEXT    NOT NULL REFERENCES slskd_searches(id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,              -- candidate position when picked
    username     TEXT    NOT NULL,              -- peer selection skips every one listed
    filename     TEXT    NOT NULL,              -- peer's remote path
    size         INTEGER NOT NULL,
    transfer_id  TEXT,                          -- slskd's, once queued
    outcome      TEXT    CHECK (outcome IN ('completed', 'noStart', 'tooSlow', 'failed')),
                                                -- NULL while running
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    CHECK ((outcome IS NULL) = (ended_at IS NULL))
);
CREATE INDEX idx_slskd_attempts_download ON slskd_attempts(download_id, username);
CREATE INDEX idx_slskd_attempts_search   ON slskd_attempts(search_id);
CREATE INDEX idx_slskd_attempts_transfer ON slskd_attempts(transfer_id);

-- only peers with a current bad record; no row = clean peer; no FK (spans downloads)
CREATE TABLE slskd_peers (
    username       TEXT    PRIMARY KEY,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    blocked_at     INTEGER                      -- NULL = not blocked
);

-- ---------------------------------------------------------------- files

-- one current file per song, from download until Navidrome indexes it
CREATE TABLE file_management (
    id              INTEGER PRIMARY KEY,
    song_id         INTEGER NOT NULL UNIQUE REFERENCES songs(id),
    source          TEXT    NOT NULL,           -- download backend the file came from
    file_location   TEXT    NOT NULL,           -- where it landed, relative to that backend's
                                                --   download dir (until moved)
    file_state      TEXT    NOT NULL DEFAULT 'downloaded' CHECK (file_state IN (
                        'downloaded', 'verified', 'tagged', 'moved', 'indexed',
                        'missing', 'deleted')),
    verification    TEXT,                       -- 'success' or an error code
    navidrome_id    TEXT,                       -- once indexed (Navidrome owns the path)
    downloaded_at   INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX idx_file_management_state     ON file_management(file_state);
CREATE INDEX idx_file_management_navidrome ON file_management(navidrome_id);

-- hand-off: a completed download becomes (or replaces) its song's current file
CREATE TRIGGER downloads_completed_handoff
AFTER UPDATE OF state ON downloads
FOR EACH ROW WHEN NEW.state = 'completed' AND OLD.state IS NOT 'completed'
BEGIN
    INSERT INTO file_management
        (song_id, source, file_location, file_state, downloaded_at, updated_at)
    SELECT r.song_id, NEW.source, NEW.file_location, 'downloaded',
           COALESCE(NEW.completed_at, unixepoch()), unixepoch()
    FROM requests r WHERE r.id = NEW.request_id AND r.song_id IS NOT NULL
    ON CONFLICT (song_id) DO UPDATE SET
        source = excluded.source,
        file_location = excluded.file_location,
        file_state = 'downloaded',
        verification = NULL,
        navidrome_id = NULL,
        downloaded_at = excluded.downloaded_at,
        updated_at = excluded.updated_at;
END;

-- one row per tagging attempt; failTag lives only here
CREATE TABLE tagging (
    id               INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES file_management(id) ON DELETE CASCADE,
    status           TEXT    NOT NULL CHECK (status IN ('tagged', 'failTag')),
    error            TEXT,
    mb_recording_id  TEXT,
    mb_release_id    TEXT,
    tags_written     TEXT,                      -- JSON
    tagged_at        INTEGER NOT NULL
);
CREATE INDEX idx_tagging_file ON tagging(file_id);

-- ---------------------------------------------------------------- setup

CREATE TABLE setup_state (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);
