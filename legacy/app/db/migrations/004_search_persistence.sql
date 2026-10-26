-- P6.5-4: durable search job + peer-response persistence
--
-- search_jobs / search_responses / worker_state back the previously
-- in-memory dicts in SlskdSearch and SlskdDownload, which were wiped on
-- every restart — breaking DownloadMonitor's alternative-peer retry and
-- losing the RecPuller's pull schedule. The user-facing `searches` table
-- is untouched: it still holds only user-initiated search history.

-- Every search job (user + rec), so get_results()/get_status() can
-- reconstruct jobs after a restart instead of 404ing.
CREATE TABLE IF NOT EXISTS search_jobs (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    artist TEXT,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL
);

-- Raw slskd peer responses per search, stored so alternative-peer retry
-- (DownloadMonitor._pick_alternative_peer) has candidates to pick from
-- even after a restart wipes slskd's own in-memory search retention.
CREATE TABLE IF NOT EXISTS search_responses (
    search_id TEXT NOT NULL,
    username TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (search_id, username)
);
CREATE INDEX IF NOT EXISTS idx_search_responses_search_id
    ON search_responses(search_id);

-- Generic worker state (RecPuller last-run timestamps, etc.).
CREATE TABLE IF NOT EXISTS worker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
