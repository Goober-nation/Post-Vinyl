-- Drop config / users / sessions — dead tables, never wired to any code path.
--
-- 2026-09-24: confirmed via grep (no "FROM config"/"INTO config",
-- "FROM users"/"INTO users", "FROM sessions"/"INTO sessions" anywhere in
-- app/) that none of these three tables are read or written by any current
-- code:
--   * config    — Config is TOML-backed (config.toml + .env), never DB-backed.
--     This table has always been empty in the live stack.
--   * users     — scaffolding for a login system that was never built
--     (auth today is BasicAuthMiddleware, credentials from config/.env).
--     Empty in the live stack.
--   * sessions  — same login system's session table, FK'd to users. Also
--     empty. Dropped first since it references users.
--
-- Part of the pre-core-rebuild database cleanup (see 013 for the
-- recs/sync/playlist archival). If a real login system (issue #29) gets
-- built later, it gets a fresh, purpose-built schema at that time rather
-- than reviving this one.

DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS config;
