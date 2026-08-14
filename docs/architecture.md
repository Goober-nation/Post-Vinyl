# Architecture

Post-Vinyl is a FastAPI app (`app/main.py`) that coordinates four external systems — slskd, Navidrome,
beets, ListenBrainz/MusicBrainz — through a set of services, background workers, and a small SQLite
database of its own bookkeeping. This document describes how those pieces fit together. For the
individual HTTP endpoints, see [`docs/api.md`](api.md).

## Ownership model

Post-Vinyl does not duplicate state that another service already owns:

- **slskd** owns search results and transfer state (with retention, so Post-Vinyl can re-read a completed
  search later instead of re-searching).
- **Navidrome** owns the library and playlists.
- **ListenBrainz** owns recommendations.
- **Post-Vinyl** owns only its own bookkeeping: search headers, download rows, and worker state, in
  `app/db/*` (SQLite, under `paths.data_dir`).

This matters for a few specific behaviors: retrying a failed download re-reads the *same* slskd search
by ID and picks a different peer, rather than starting a new search; and a search's raw responses are
never independently persisted by Post-Vinyl beyond a short-lived header row.

## Request lifecycle

`app/main.py` builds the FastAPI app: constructs each service, stores them on `app.state.services`,
mounts `BasicAuthMiddleware` if `auth.enabled`, registers the 8 routers, and mounts the static frontend.
On startup (`lifespan()`), it initializes the DB schema (running any pending migrations from
`app/db/migrations/`) and starts five background workers: `DownloadMonitor`, `RecPuller`, `LoveSync`,
`TrashPurge`, `HistoryCleaner`. On shutdown it signals `EventHub.signal_shutdown()` and joins all
workers against a shared 5s deadline. `app/dependencies.py` provides the FastAPI DI functions that pull
services off `app.state`; `get_config()` also triggers `Config.reload_if_changed()`, which is how
non-secret config edits take effect without a restart.

Errors raised anywhere as `MusicaError` subclasses (`app/exceptions.py`) are mapped to HTTP status codes
centrally (`_musica_error_status`) and returned as `{"error": {code, message, details}}`.

## Services (`app/services/`)

Services follow an interface/implementation split: `app/services/interfaces/` defines ABCs and shared
dataclasses (`SearchService`, `DownloadService`, `RecommendationService`, `FeedbackService`,
`MusicBrainzService`); concrete classes implement them against the real integrations. This is what lets
routes and workers depend on behavior, not on slskd/Navidrome/MusicBrainz specifics directly.

| Service | Role |
|---|---|
| `search.py` (`SlskdSearch`) | Drives Soulseek search via slskd's "cancel-to-flush" pattern (required since slskd 0.26.0 — responses return `[]` until a search is cancelled or completes). Rehydrates in-flight jobs from the DB on restart. |
| `search_limiter.py` (`SearchRateLimiter`) | Sliding-window rate limit on new searches — see [Search connection behavior](#search-connection-behavior) below for why this exists. |
| `query_builder.py` | Pure functions turning a track+artist into short slskd queries. Empirically, 2-word queries return results on Soulseek; 3+-word combined queries tend to return zero — this is baked into `build_search_queries`. |
| `download.py` (`SlskdDownload`) | Queues/tracks/retries/cancels transfers via slskd's REST API; bad-peer blocking (`mark_peer_bad`/`unblock_peer`). |
| `track_requester.py` | Shared, DB-free "search-and-queue driver" (`run_ladder`, `is_viable_candidate`) used by both `RecPuller` and the MusicBrainz resolve job, so the retry-ladder logic can't drift between the two callers. |
| `beets.py` (`BeetsService`) | Runs `beet import` as a subprocess against one of five beets profiles (searches / library / discovery_familiar / discovery_new_releases / discovery_exploration) to tag, rename, and move a completed download. Also handles cross-profile album-group consolidation (e.g. a track downloaded via manual search and later via recs, deduped into one album folder). |
| `musicbrainz_client.py` (`MusicBrainzClient`) | HTTP client for MusicBrainz's API — search/lookup/browse, rate-limited to 1 req/sec with a TTL cache. `resolve_canonical(title, artist)` is the piece that pins a download to the actual recording the user meant, not whatever a Soulseek peer's own file tags claim. |
| `library.py` / `navidrome_library.py` | `LibraryService` interface and its Navidrome (Subsonic API) implementation — playlists, starred tracks, ratings, scans, and `get_song_real_path()` (native token lookup, since Subsonic's reported `path` is tag-synthesized, not a real filesystem path). |
| `recommendation.py` (`ListenBrainzRecs`) | Fetches, classifies, and queues recs across the three categories — see [Recommendations](#recommendations). |
| `feedback.py` (`ListenBrainzFeedback`) | Sends love/hate feedback to ListenBrainz. |
| `rec_playlist.py` (`RecPlaylistService`) | Gets a completed rec download into its category's Navidrome playlist; shared by `DownloadMonitor`'s on-completion hook and `RecPuller`'s retry pass. |
| `health.py` | Free-function connectivity checks (`check_slskd`, `check_navidrome`, `check_listenbrainz`, `check_all`) and `reconnect_slskd()`. |
| `download_data.py`, `recs_data.py` | Thin service-layer wrappers over the DB stores, used by routes so DB access doesn't happen directly in route handlers. |

## Workers (`app/workers/`)

All workers share the same shape: `start()`/`stop()`/`request_stop()`/`join()` around a daemon thread,
plus a synchronous, independently testable core method (`run_once`, `poll_once`, etc.).

| Worker | Cadence | Responsibility |
|---|---|---|
| `download_monitor.py` (`DownloadMonitor`) | every `download.check_interval` (default 15s) | Polls slskd for transfer state; drives the whole download lifecycle — tracking, retry, orphan reconciliation, stale-pending reaping, beets import handoff, missing-source handling, SSE events, rec-playlist sync on completion. |
| `rec_puller.py` (`RecPuller`) | tick every `interval` (config), per-category day-granularity due-check | Fetches due categories, classifies against the library, adds already-owned tracks to the playlist, queues downloads for the rest. Paced 2s per track to avoid a Soulseek connection storm. Also handles manual pulls via `trigger_pull(categories)`. |
| `love_sync.py` (`LoveSync`) | every `sync.interval_hours` (default 12h) + once at startup | Navidrome-starred songs → rating 5 + ListenBrainz love. One-way (loves only, no unstar handling). |
| `trash_purge.py` (`TrashPurge`) | every `sync.interval_hours` (default 12h) + once at startup | Navidrome Trash playlist entries → ListenBrainz hate + delete file from disk + remove from playlist; also sweeps stranded downloads and triggers a Navidrome scan when files were deleted. |
| `history_cleaner.py` (`HistoryCleaner`) | every `download.history_clear_interval_minutes` (default 15, 0 disables) + once at startup | Deletes slskd-side terminal transfer/upload history to prevent slskd stack congestion (found live 2026-08-14 — see below). Never touches Post-Vinyl's own DB rows. |
| `mb_resolver.py` | one-shot per request | Not a persistent worker — `start_resolve_job(scope, mbid, ...)` launches a one-shot daemon thread per MusicBrainz download request (recording or album), running the same search ladder as `RecPuller`, paced 2s/track, progress reported via SSE (`mb.resolve_started` / `mb.track_queued` / `mb.track_failed` / `mb.resolve_completed`). |

## Recommendations

Three categories, each independently enabled/scheduled/counted in `config.toml`'s `[recs]` and
`[fresh_picks]` sections, each with its own pull/dedup mechanism because their upstream ListenBrainz
endpoints behave differently:

| Category | Source | Pool | Rotation signal | Dedup |
|---|---|---|---|---|
| **Comfort Zone** | `/1/cf/recommendation/user/{user}/recording` | 1000 tracks | `last_updated` changes (tied to LB model retrains — no fixed schedule) | offset pagination through the pool |
| **Fresh Picks** | `/1/explore/fresh-releases/` | global feed (not personalized, by design) | continuous | max `release_date` seen |
| **Deep Cuts** | `/1/user/{user}/playlists/recommendations` | ~25-50 tracks per weekly playlist | a new playlist UUID appears | ingested-UUID set |

All three ListenBrainz endpoints are public and work without an auth token. Deep Cuts playlists are
generated upstream by ListenBrainz's own weekly job (troi's `PeriodicJamsPatch`) — track volume there
is entirely ListenBrainz-controlled, not something Post-Vinyl can increase.

A rec that classifies as already-in-library gets added straight to its category playlist; one that
doesn't goes through the same search-and-download pipeline as a manual search, via
`track_requester.run_ladder()`.

## Import pipeline: from transfer to library

1. A search (manual, rec pull, or MusicBrainz resolve) queues a download through `SlskdDownload`.
2. `DownloadMonitor` polls slskd until the transfer reaches a terminal state.
3. On completion, `DownloadMonitor._resolve_intent()` recovers the original title/artist the user
   actually asked for — from `SearchStore` for manual downloads, or `RecsStore.get_rec_by_search_id()`
   for recs (recs never get a `searches` header row).
4. `BeetsService.import_file()` resolves that title/artist to a canonical MusicBrainz recording via
   `resolve_canonical()`, then runs `beet import` in singleton mode, pinned to that recording with
   `--search-id <mbid> --from-scratch` when confident, plus `--set albumartist=... --set album=...`
   (singleton-mode beets matches against a *recording*, not a release, so `albumartist`/`album` are
   never populated by the match itself — they have to be forced). Low-confidence or missing
   title/artist falls back to unconstrained beets matching rather than guessing.
5. On successful import, `RecPlaylistService.add_downloaded_to_playlist()` adds rec downloads to their
   category's Navidrome playlist.

This exists because beets, left to its own devices, will tag a file by whatever identity a Soulseek
peer's file tags happen to claim — which can be badly wrong (a live bootleg mislabeled as the studio
track, a cover band's recording instead of the original). `resolve_canonical()`'s artist-discrimination
is still imperfect in edge cases (a known false-positive: a title-only match once resolved a Madvillain
track to an unrelated cover band because artist-credit wasn't weighted strongly enough) — see
`agents_memory/topics/` for the live-verification history of this pipeline if debugging a wrong-tag
report.

## Search connection behavior

Soulseek search is broadcast network-wide: every peer holding a match dials in to the searcher.
Measured live, **one search opens roughly 6,000-7,000 host-visible sockets** (about 2,800 real slskd-side
peer connections, inflated further by Docker Desktop's userspace port-forwarder lagging behind slskd's
own ~1-minute cleanup). Neither narrowing the query nor lowering slskd's `responseLimit` meaningfully
reduces this — `responseLimit` is local bookkeeping only; slskd still accepts every peer connection to
read its response before discarding the excess. The only variable that scales is **search frequency**.

Post-Vinyl addresses this with:
- **Query cache** (`search.query_cache_ttl_seconds`, default 600s) — an identical query within the
  window is served from the prior search's already-fetched responses, no new slskd call.
- **Sliding-window rate limiter** (`search.rate_limit_max_searches` per
  `search.rate_limit_window_seconds`) — blocks new searches beyond the limit, waiting up to
  `search.rate_limit_wait_timeout_seconds` before failing fast with `SearchRateLimitedError` (HTTP 429).
- Recs and MusicBrainz-resolve pulls are paced (2s between tracks) rather than firing every search at
  once.

If the whole Docker stack seems to hang after a burst of searches, this connection saturation is the
first thing to check, not a Post-Vinyl or slskd bug — see `HistoryCleaner` above for the closely related
transfer-history-accumulation issue.

## Configuration model

Non-secret settings live in `config.toml` (grouped by `[server]`, `[paths]`, `[navidrome]`, `[slskd]`,
`[listenbrainz]`, `[search]`, `[download]`, `[recs]`, `[fresh_picks]`, `[sync]`, `[logging]`, `[auth]`,
`[beets]`, `[musicbrainz]`) and are hot-reloadable — `POST /api/config` writes them back to the file,
and `Config.reload_if_changed()` (called on every request via the `get_config` dependency) picks up
external edits too. Secrets (`NAVIDROME_PASSWORD`, `SLSKD_API_KEY`, `LISTENBRAINZ_TOKEN`, etc.) live
only in `.env`/environment variables, are never written to `config.toml`, and require a restart to take
effect. See [`docs/deployment.md`](deployment.md) for the full field reference.

## Database

SQLite under `paths.data_dir`, schema-versioned via `app/db/migrations/*.sql` applied in order and
tracked in an `applied_migrations` table (`Database._run_migrations()`). Four stores, one per table
family:

- `SearchStore` — the `searches` table (search job persistence/rehydration).
- `DownloadStore` — the `downloads` table (transfer tracking, retry counts, peer-blocking, import state).
- `RecsStore` — the `recs` table plus per-category worker state and the Deep Cuts ingested-UUID pool.
- `SyncStore` — the `sync_state` table (love/hate feedback sync status per song).
