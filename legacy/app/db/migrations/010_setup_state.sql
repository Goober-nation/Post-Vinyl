-- First-run setup wizard state — small generic key/value store, separate
-- from recs' own worker-state table since this is app-level onboarding
-- state, not a worker's bookkeeping.
CREATE TABLE IF NOT EXISTS setup_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
