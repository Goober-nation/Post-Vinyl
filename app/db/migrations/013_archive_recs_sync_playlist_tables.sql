-- Drop the recommendations / sync / playlist-automation tables.
--
-- 2026-09-24: the recommendations pipeline (RecPuller, ListenBrainzRecs),
-- the Navidrome sync workers (LoveSync, TrashPurge), and the auto-playlist
-- services (RecPlaylistService, SearchesPlaylistService, playlist_registry)
-- were disconnected from the running app ahead of the core search/download
-- rebuild, so they can't block or interfere with that work. This is not a
-- deletion of the subsystem — its code was moved intact (via `git mv`, so
-- history is preserved) to archive/pre-rebuild-recs-sync-playlists/, not
-- deleted, and can be restored later. This migration only drops the tables
-- that belonged exclusively to that now-archived code.
--
-- Core tables this subsystem never owned — searches, downloads, peers,
-- download_retry_attempts, setup_state, users, sessions, config — are
-- untouched here; they stay and will be revised separately, not as part of
-- this archival pass. The downloads table's is_rec_download /
-- is_library_download columns also stay, now simply unpopulated by any
-- caller — see download_store.py.

DROP TABLE IF EXISTS recommendations;
DROP TABLE IF EXISTS sync_state;
DROP TABLE IF EXISTS rec_category_state;
DROP TABLE IF EXISTS deep_cuts_playlists;
DROP TABLE IF EXISTS deep_cuts_pool;
DROP TABLE IF EXISTS playlist_registry;
DROP TABLE IF EXISTS worker_state;
