-- Users are matched by Navidrome's permanent id, so a rename there keeps their
-- history. A deleted user's row stays, hidden, because requests point at it.
ALTER TABLE users ADD COLUMN nd_id TEXT;
ALTER TABLE users ADD COLUMN deleted_at INTEGER;
CREATE UNIQUE INDEX idx_users_nd_id ON users(nd_id) WHERE nd_id IS NOT NULL;
