# API Reference

All endpoints are under `/api` (MusicBrainz endpoints are under `/api/musicbrainz`). Errors return
`{"error": {"code": ..., "message": ..., "details": ...}}` with an HTTP status mapped from the raised
`MusicaError` subtype (see `app/exceptions.py`) — typically 400 for bad input, 404 for not-found, 429
for rate limits, 503 for an unreachable upstream service, 500 otherwise. If HTTP Basic Auth is enabled
(`MUSICA_AUTH_USERNAME`/`PASSWORD` set), every route below requires it.

Live status (search progress, transfer state changes, recs pulls, MusicBrainz resolve jobs) is also
pushed over `GET /api/events`, an SSE stream — poll the REST endpoints for current state, subscribe to
events for live updates without polling.

## System

| Method & Path | Purpose |
|---|---|
| `GET /api/system/ping` | Liveness check (excluded from the OpenAPI schema). |
| `GET /api/system/status` | Overall system/service health (slskd, Navidrome, ListenBrainz reachability). |
| `POST /api/system/restart` | Restart the app process. |
| `POST /api/system/sync` | Trigger `LoveSync`/`TrashPurge` immediately, outside their normal interval. |
| `POST /api/system/slskd/reconnect` | Force a slskd reconnect attempt. |
| `POST /api/system/consolidate` | Trigger beets cross-profile album consolidation (dedupe albums split across profile folders). |
| `POST /api/system/listenbrainz/check` | On-demand ListenBrainz connectivity check. |
| `POST /api/system/stop-slskd-activity` | Stop current slskd search/transfer activity. |
| `GET /api/logs` | Tail app logs as plain text. Query: `limit` (default 100, range 1-1000). |

## Setup

First-run wizard backend, driving the in-app setup wizard (`app/static/js/setup.js`) that opens
automatically on first visit — see [deployment.md](deployment.md) for the guided flow these routes
back.

| Method & Path | Purpose |
|---|---|
| `GET /api/setup/status` | Whether the wizard/tutorial are done and which accounts (Navidrome, slskd, ListenBrainz) are configured. |
| `POST /api/setup/navidrome` | Create Navidrome's first admin account, or verify+save credentials for an existing one. |
| `POST /api/setup/slskd` | Save the Soulseek login to `.env`. Requires a manual `docker compose up -d slskd` to take effect — musica has no Docker socket access to do this itself. **Not** `docker compose restart slskd` — restart reuses the container's existing environment rather than re-reading `.env`, so the old credentials silently stick around and slskd never connects. |
| `GET /api/setup/slskd/check` | Whether slskd connected after that recreate, and why not if it didn't (reads slskd's own logs for a credential-failure signature — Soulseek has no separate username-availability check, logging in *is* how a new username registers). |
| `POST /api/setup/complete` | Mark the wizard finished. |
| `POST /api/setup/tutorial/dismiss` | Mark the tutorial dismissed. |
| `POST /api/setup/rerun` | Reset the wizard/tutorial flags so the flow replays; already-saved credentials are untouched. |

## Config

| Method & Path | Purpose |
|---|---|
| `GET /api/config` | Full non-secret config as JSON. |
| `POST /api/config` | Update non-secret config values; hot-reloadable, takes effect without a restart. Writes to `config.toml` under a backup guard. |
| `POST /api/config/secrets` | Update `.env`-stored secrets (Navidrome/slskd/ListenBrainz credentials, auth). **Requires a restart** to take effect. |

## Search

| Method & Path | Purpose |
|---|---|
| `POST /api/search` | Start a Soulseek search. Body: query text (and optional artist). Returns 201 with the search job. Subject to the query cache and rate limiter — see [architecture.md#search-connection-behavior](architecture.md#search-connection-behavior). |
| `GET /api/searches` | List all search jobs, newest first. |
| `GET /api/searches/{search_id}` | Search job detail plus results. |
| `GET /api/searches/{search_id}/progress` | Non-flushing live progress poll (safe to call repeatedly while a search is in flight — unlike the result-fetch path, does not force a cancel-to-flush). |
| `POST /api/searches/{search_id}/cancel` | Cancel a search (triggers cancel-to-flush so results become readable). |

## Downloads

| Method & Path | Purpose |
|---|---|
| `GET /api/transfers` | List active/recent download transfers. |
| `POST /api/queue` | Queue one or more files for download from a peer, given a search result. Returns 201. Validates the destination path. |
| `POST /api/queue/retry/{transfer_id}` | Retry a failed download — re-reads the same stored search results and re-picks a peer; does **not** start a new Soulseek search. |
| `DELETE /api/transfers/{transfer_id}` | Cancel an active transfer. |
| `DELETE /api/transfers` | Bulk-delete finished transfer records from Post-Vinyl's own bookkeeping (does not affect files already imported). |

## Recommendations

| Method & Path | Purpose |
|---|---|
| `GET /api/recs/status` | Per-category status counts (Comfort Zone / Fresh Picks / Deep Cuts). |
| `POST /api/recs/pull` | Trigger a manual pull, optionally scoped to specific categories. Runs the same pipeline as the scheduled `RecPuller` tick. |
| `POST /api/recs/settings` | Update recs config: per-category enable flags, pull intervals, target counts. |
| `GET /api/recs/pending` | List recs currently queued/pending download. |
| `POST /api/recs/pending/cancel-queued` | Cancel all currently-queued rec downloads. |
| `POST /api/recs/abort` | Abort an in-progress pull. |

## Library

| Method & Path | Purpose |
|---|---|
| `POST /api/library/scan` | Trigger a Navidrome library scan. |
| `GET /api/playlists` | List Navidrome playlists. |
| `POST /api/playlists/{playlist_id}/sync` | Sync a playlist — retries adding any downloaded-but-not-yet-playlisted tracks. |

## MusicBrainz

Prefix: `/api/musicbrainz`.

| Method & Path | Purpose |
|---|---|
| `GET /search` | Unified adaptive search across recordings/albums/artists. |
| `GET /search/recordings` | Search recordings only. |
| `GET /search/albums` | Search release groups (albums) only. |
| `GET /search/artists` | Search artists only. |
| `GET /artists/{mbid}/albums` | An artist's release groups (discography). |
| `GET /albums/{mbid}/tracks` | Canonical ordered track list of a release group. |
| `POST /recordings/{mbid}/download` | Start an async resolve-and-queue job for one recording. Returns 202 with `{started, job_id}`; progress via `GET /api/events` (`mb.resolve_started`/`mb.track_queued`/`mb.track_failed`/`mb.resolve_completed`). |
| `POST /albums/{mbid}/download` | Same, for an entire album/release-group — queues every track on it. |

## Events (SSE)

| Method & Path | Purpose |
|---|---|
| `GET /api/events` | Server-Sent-Events stream of live status: search progress, transfer state changes, recs pull activity, MusicBrainz resolve-job progress. Optional `types` query param to filter to specific event types. |
