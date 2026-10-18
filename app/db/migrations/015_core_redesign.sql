-- Core DB redesign (#67): request → slskd search → download → file lifecycle.
--
-- 2026-10-03: replaces searches / downloads / download_retry_attempts / peers
-- with the schema designed in agents_memory/topics/core-db-redesign.md.
-- Starts fresh by decision — no rows are carried over.
--
-- Shape:
--   origins (+ catalog_searches)  where a request came from (optional)
--   requests                      one row per track asked for
--   slskd_searches                one row per actual slskd search
--   downloads                     the network transfer, one row per requested
--                                 file, kept through every retry
--   download_attempts             every peer tried for a download
--   file_management               what happened to the file after it arrived;
--                                 created by trigger, outlives `downloads`
--   mb_provenance                 MusicBrainz recording a placed file was
--                                 requested as
--   peers                         peers with a *current* bad record only
--
-- Foreign keys only work with `PRAGMA foreign_keys = ON` on the connection,
-- which Database.connect() sets. It is switched off for the drops below so
-- dropping the old tables can't trip constraints mid-script.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS download_retry_attempts;
DROP TABLE IF EXISTS downloads;
DROP TABLE IF EXISTS searches;
DROP TABLE IF EXISTS peers;

-- ── Origins ──────────────────────────────────────────────────────────────

CREATE TABLE origins (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL                -- catalog_search | rec | …
);

-- Typed MusicBrainz-tab searches (history). Shares its id with `origins`;
-- delete through `origins` so both rows go.
CREATE TABLE catalog_searches (
    id            INTEGER PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
    query         TEXT    NOT NULL,
    kind          TEXT    NOT NULL,    -- recording | album | artist
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL
);
-- Repeat queries collapse into one row (case-insensitive).
CREATE UNIQUE INDEX idx_catalog_searches_query ON catalog_searches(lower(query), kind);

-- ── Requests ─────────────────────────────────────────────────────────────

CREATE TABLE requests (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,    -- slskd | catalog | rec | …
    origin_id     INTEGER REFERENCES origins(id) ON DELETE SET NULL,
    query         TEXT,
    mbid          TEXT,                -- recording MBID for catalog/rec
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL,
    CHECK (source <> 'slskd' OR origin_id IS NULL)
);
CREATE INDEX idx_requests_origin ON requests(origin_id);
CREATE INDEX idx_requests_last_used ON requests(last_used_at);
-- A repeated typed Soulseek search reuses its request.
CREATE UNIQUE INDEX idx_requests_slskd_query
    ON requests(lower(query)) WHERE source = 'slskd';

-- ── slskd searches ───────────────────────────────────────────────────────

CREATE TABLE slskd_searches (
    id          TEXT    PRIMARY KEY,   -- slskd's search id, always real
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    query       TEXT    NOT NULL,      -- exact text sent to slskd
    artist      TEXT,                  -- client-side filter
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_slskd_searches_request ON slskd_searches(request_id);

-- ── Downloads ────────────────────────────────────────────────────────────

CREATE TABLE downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    search_id     TEXT    NOT NULL REFERENCES slskd_searches(id) ON DELETE CASCADE,
    slskd_id      TEXT,                -- current attempt's transfer id, once slskd reports it
    username      TEXT    NOT NULL,    -- current peer
    filename      TEXT    NOT NULL,    -- current peer's remote path
    size          INTEGER,
    state         TEXT    NOT NULL,    -- queued | downloading | completed | failed | cancelled
    created_at    INTEGER NOT NULL,    -- start of the retry chain
    completed_at  INTEGER
);
CREATE INDEX idx_downloads_request  ON downloads(request_id);
CREATE INDEX idx_downloads_search   ON downloads(search_id);
CREATE INDEX idx_downloads_state    ON downloads(state);
CREATE INDEX idx_downloads_slskd_id ON downloads(slskd_id);

-- Every peer tried for a download; the first row is the original request.
-- Retry count = COUNT(*) - 1.
CREATE TABLE download_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id   INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    username      TEXT    NOT NULL,
    attempted_at  INTEGER NOT NULL,
    success       BOOLEAN NOT NULL DEFAULT 0   -- slskd accepted the queue request
);
CREATE INDEX idx_download_attempts ON download_attempts(download_id, username);

-- ── File management ──────────────────────────────────────────────────────

CREATE TABLE file_management (
    id             INTEGER PRIMARY KEY,
    source         TEXT    NOT NULL,
    username       TEXT    NOT NULL,   -- with filename, locates the file under
    filename       TEXT    NOT NULL,   --   downloads/complete/soulseek/<username>/
    stage          TEXT    NOT NULL,   -- pending | moved | missing | unindexed
    placed_path    TEXT,               -- relative to the library root, incl. final filename
    navidrome_id   TEXT,               -- Navidrome media_file.id once indexed
    downloaded_at  INTEGER NOT NULL
);
CREATE INDEX idx_file_management_stage     ON file_management(stage);
CREATE INDEX idx_file_management_navidrome ON file_management(navidrome_id);

CREATE TABLE mb_provenance (
    file_management_id  INTEGER PRIMARY KEY REFERENCES file_management(id) ON DELETE CASCADE,
    mb_recording_id     TEXT NOT NULL
);

-- A download reaching 'completed' hands its file over to file_management.
-- Rows are always inserted as 'queued', so an UPDATE trigger covers every
-- completion.
CREATE TRIGGER downloads_completed_handoff
AFTER UPDATE OF state ON downloads
FOR EACH ROW WHEN NEW.state = 'completed' AND OLD.state IS NOT 'completed'
BEGIN
    INSERT INTO file_management (source, username, filename, stage, downloaded_at)
    SELECT r.source, NEW.username, NEW.filename, 'pending',
           COALESCE(NEW.completed_at, CAST(strftime('%s', 'now') AS INTEGER))
    FROM requests r WHERE r.id = NEW.request_id;

    INSERT INTO mb_provenance (file_management_id, mb_recording_id)
    SELECT last_insert_rowid(), r.mbid
    FROM requests r WHERE r.id = NEW.request_id AND r.mbid IS NOT NULL;
END;

-- ── Peers ────────────────────────────────────────────────────────────────

-- Only peers with a current bad record; no row = clean peer.
CREATE TABLE peers (
    username       TEXT    PRIMARY KEY,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    blocked_at     INTEGER             -- NULL = not blocked
);

PRAGMA foreign_keys = ON;
