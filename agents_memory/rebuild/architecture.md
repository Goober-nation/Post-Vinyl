# Architecture: target structure (decided 2026-10-26)

The one page that ties the rebuild together. Detail lives in the other notes in this folder:
`core-db-redesign.md` (schema), `users-auth.md` (auth), `design-notes-compilation.md` (search &
matching), `search-test-v2.md` (benchmark rerun), `work-plan.md` (rules, work order, known bugs).

## Decisions (user, 2026-10-26)
- **Layered** layout (not per-feature).
- **ABCs on every client, and on the services that routes and workers call.** `core/` functions,
  stores and internal helpers stay plain. Each ABC has a fake for tests.
- **Threads + sync code** stay (daemon-thread workers, `requests`). Clients sit behind ABCs, so a
  later move to asyncio only touches clients and workers.
- `architecture.md` written directly (no mapping pass of the current code first).

## Rebuild approach (user, 2026-10-27)
- **From scratch, in place**: the old `app/` moves to **`legacy/`** (reference only, never run,
  deleted when the rebuild is done); a fresh `app/` is written to this architecture. Survivors
  (§5 "stays") are ported deliberately from `legacy/`, not kept by default.
- **Branches**: all work on `rebuild/*` branches, branched from and merged into
  **`rebuild/base`**; `rebuild/base` → `main` only when the rebuild is done. `dev` and `main` keep
  the old app untouched meanwhile.
- **Database**: **one clean initial migration** with the full new schema (folds 001–016; no old
  data carried over).
- **No frontend until the core backend is done** (milestone 7): testing through FastAPI's
  **`/docs`** (Swagger UI), allowed in development only (exempt from default deny there). The
  frontend is milestone 8 (user, 2026-10-28); milestones 9–11 each add their own page.

## Swappable sources (user, 2026-10-28)
- **Download sources are pluggable.** Core asks a generic **`DownloadSource`** to "get this
  song" (song data from the catalog) and only tracks the job from outside (requested, done,
  failed, file path). It knows nothing about how the source finds or fetches files.
- **slskd is one self-contained download backend**: a thin `clients/slskd.py` (raw HTTP, no
  decisions) plus an slskd service that owns every Soulseek decision: readiness (logged in +
  network parent), search rate cap, throttle detection, query strategy, picking files and
  peers, stall rule, retries. It never calls MusicBrainz/Deezer itself. Another backend plugs in
  as another `DownloadSource`.
- **Catalog sources are swappable too**: MusicBrainz, ListenBrainz and Deezer sit behind a
  generic `CatalogSource` ABC (search, lookup by id/ISRC) where a caller may use any of them.
- Catalog clients stay thin (user, 2026-10-28, #81): legacy's "pick the canonical release" and
  "looks like a variant" logic is a decision and moves to core / the catalog service
  (milestones 5/6); the MusicBrainz response cache is dropped (resolved songs live in the DB).
- **ListenBrainz: one ABC, fully implemented** (user, 2026-10-28, #83): recording metadata,
  name → MBID lookup, token validation. No split into capability interfaces while ListenBrainz
  is the only source for them.
- `DownloadSource` and the slskd service land in milestones 5/6; milestone 3 builds the thin
  clients only. Where §2–4 below name `search`/`download` services, read them as the slskd
  backend's internals.

## Source switches (user, 2026-10-29)
- Applies to **every kind of source**: catalog (`CatalogSource`: MusicBrainz, ListenBrainz,
  Deezer), download (`DownloadSource`: slskd, later backends) and request sources (Postradio
  recs, Yandex playlists).
- **Two levels, no admin UI**: the global config file in the app folder has one ordered list
  per kind (catalog, download, request sources). **A source missing from the list is off; the
  list order is the priority order** (user, 2026-10-29). Each user may switch off any listed
  source and **reorder** them for themselves in `user_settings`; a user can never add a source
  the config doesn't list.
- **Adding or swapping a source = a new class behind the existing ABC + a name in the config
  list**; no service, core or route code names a concrete source. Services get a registry of
  enabled sources (built in `main.py` from config) and ask it per user ("enabled sources for
  user X, in their order"). With MusicBrainz off, the first enabled catalog source is canonical
  (user, 2026-10-29).
- **The config file documents its allowed values**: each source list carries a comment naming
  every possible source; adding a source updates that comment in the same change. Startup
  rejects an unknown source name with a clear error.
- **Turning a source off never deletes data**: songs, identities, requests and files it
  produced stay; it just isn't asked any more. A request whose only download source is off
  waits (stays in its state) rather than failing.
- **Every config file is YAML** (user, 2026-10-29): `config/post-vinyl.yaml` at
  `PV_CONFIG_PATH`, documented by `config/post-vinyl.example.yaml`; secrets stay in `.env`.
  The env-only startup values move into it with the config redesign.
- **Catalog service (#95, user 2026-10-29)**: a typed search goes to the user's catalog sources in
  order and the **first source that answers with results** is shown; `resolve` stores **only the
  pick** (song + the picked source's id; linking to other sources and choosing a release is
  tagging's job). **ListenBrainz** is in the catalog list as the **name → MBID fallback** for
  request sources (Yandex/Postradio give title + artist); typed searches skip it.
- **Requests service (#96, user 2026-10-29)**: every create is a **new row**, even when the user
  already has a request for the song; search/download deal with doubles. State changes are
  written only if the state is unchanged since read (`set_track_state(expected=…)`).
- **Review fixes (user, 2026-10-29)**: no enabled catalog source → soft error "catalog search
  is unavailable without sources" (logged; shown as is by the frontend later). Users reorder
  sources only by moving fixed options in the frontend; a malformed stored preference counts
  as unset. Paged lists: default 20, max 100. Every service fake must behave like the real
  service (`tests/test_services/test_fakes_match.py` runs the same scenarios on both).
  `resolve` returns a song already in the DB without checking its source is still enabled
  (no call to the source is made; user, 2026-10-29).
  Transactions that read then write use `Database.transaction(write=True)` (BEGIN IMMEDIATE).
- **Track result order (#98, user 2026-10-29)**: `core/catalog_order.py`, applied to each
  source's answer (one page of 100): excluded version not in the query last → has an Official
  plain Album/Single/EP release first → source score (a source with no scores keeps its own
  order) → earliest first-release date → id. Typed fields are trimmed; an empty search is
  refused before anything is recorded.
- **DownloadSource + dispatch (#99, 2026-10-29)**: `DownloadSource` = `start(request, song)` +
  `cancel(request_id)`. `start` returns at once (work on the source's own thread); the source
  claims the request with its guarded `requested`/`reRequested` → `searching` write, so a
  second dispatch does nothing; it owns the track state from there and writes its `downloads`
  rows. `services/dispatch.py` hands a request to the user's first enabled download source
  (none → the request waits, nothing written), cancels through every source, and reads status
  from what the source wrote. Falling back to the next source after `notFound` is not built.
- **slskd gate (#100, user 2026-10-29)**: `services/slskd_gate.py`, in memory. One search at a
  time, ≤ `searches_per_minute` (4) starts in any 60 s. Not ready (logged out / no parent) →
  wait with no limit. Parent lost during a search → result dropped, same query again. **Empty
  results need confirming before `notFound`**: trusted if any search came back with results
  since; otherwise the throttle check search runs (`"2020"`, healthy ≥ 300); a failed check
  pauses searching 10 min and the caller reruns its queries. Advanced values in the YAML
  `slskd` section. (Replaces the "5 zero results in a row" trigger: with a 3-query ladder a
  request could end `notFound` on untrusted zeros before 5 accumulated.)
- **slskd search (#101, user 2026-10-29)**: `services/slskd_search.py` + `slskd_source.py`
  (claims the request, one worker thread, FIFO). `songs.query_title` / `songs.main_artist` hold
  `core/normalize` output computed at `resolve` from the catalog's credit parts (the joined
  credit line can't be split back reliably; with it the matcher rejected real files as
  `no_artist`). Added to `001_initial.sql` directly: nothing from the rebuild is deployed. A
  helpful peer's file: lossless first, then bitrate, then size. Candidates are never stored;
  `candidates()` re-reads the request's slskd searches by id.
- **Loop review fixes (user, 2026-10-29)**: **cancel = drop** (the request stays as `dropped`
  history; the search checks the state before every query, including the first). New
  transition **`searching` → `requested`** (`states.release`): a crashed search is released
  and re-queued **once**, a second crash ends it `notFound` (in memory); `dispatch.resume()`
  at startup releases and re-dispatches every `searching` row. An slskd error while slskd is
  still ready is a crash, not a lost parent; a cancelled search must end within 30 s. A
  search proves slskd healthy only with ≥ `throttle_check_min_responses` (300). An endless
  throttle keeps retrying with a warning per lap (a throttle hits every request alike).
  **File pick (checked against slskd 0.26.1's own web UI, user 2026-10-29)**: the extension
  comes from the file name (`getFileName(f.filename)`; the API's `extension` is mostly empty:
  512 of 825 live files). Lossless = bit depth + sample rate reported (slskd's
  `formatAttributes` rule) or a lossless suffix. Order: lossless before lossy; lossless
  **16/44.1 (CD) first**, then unknown format, then hi-res by depth × rate; lossy by highest
  bitrate (VBR flag ignored); then size. FLACs have no `bitRate`, so the earlier "then
  bitrate" rule tied every FLAC.
  `DownloadSource.start` returns whether it claimed; dispatch raises `NotStartable` if not.
- **slskd download (#102, user 2026-10-29)**: `services/slskd_download.py` + the
  `download_monitor` worker (polls every 2 s). `downloads` row first, then queue. A candidate
  that keeps us **queued > 15 s** is `noStart` (`queue_wait_seconds`); downloading → stall rule.
  **Every failure counts** toward the ban (3 → 7 days; expiry checked lazily on lookup); a
  completed download clears the peer. ≤ 5 candidates per download, same row. With none left
  the request ends **by its last attempt**: never started → `notDownloaded`, started and broke
  → `badDownload`. Each attempt saves into its own folder `post-vinyl/<download>/<attempt>`,
  sent as slskd's batch `options.destination` (0.26.1: takes precedence over slskd's own
  subdirectory pattern; the transfer API returns no local path), so `file_location` =
  that folder + slskd's sanitized base name. The test slskd reaches Soulseek through its own
  `soulseek.connection.proxy` (local, gitignored config).
- User preference storage: setting `sources.<kind>` = `{"order": [...], "off": [...]}`; a source
  new in the config is on and goes after the user's order.
- Plan each milestone 6+ service with this in mind; the registry lands with the first service
  that needs it (catalog, #95).

## 1. Layers and dependency direction

```
routes/      HTTP only: parse the request, call a service, shape the response
workers/     timed loops (download monitor, file pipeline, cleanup)
services/    use cases: combine clients + stores + core
core/        pure logic, no I/O
stores/ + clients/   the only code that touches the DB / an outside system
```

Rules:
- A layer may only import from layers below it. `routes` and `workers` never import a client or
  a store directly, only services.
- **`core/` imports nothing from the app** (stdlib only). The benchmark scripts import it too, so
  app and benchmark never drift.
- Clients know nothing about the DB; stores know nothing about HTTP.
- **No SQL outside `stores/`.** One store per group of tables.
- Services receive their clients and stores in the constructor (built once in `main.py`, handed to
  routes through `dependencies.py`), so tests pass fakes.

## 2. Modules

### `clients/` (one per outside system; ABC + fake each)
| Module | Job |
|---|---|
| `slskd.py` | start/poll/cancel searches, fetch responses (every file, `length` field), transfers (queue, status, remove), server state |
| `navidrome.py` | Subsonic + native API: ping/verify credentials, `createAdmin`, scan + scan status, song lookup, playlists |
| `musicbrainz.py` | search + lookups, rate limit (1 req/s), cache |
| `listenbrainz.py` | metadata lookup; recommendations later |
| `deezer.py` | search, track, ISRC, charts |

### `stores/` (SQLite; tables per `core-db-redesign.md`)
| Store | Tables |
|---|---|
| `database.py` | connection, migrations, `PRAGMA foreign_keys = ON` |
| `users.py` | `users`, `sessions`, `user_settings` |
| `songs.py` | `songs`, `song_identity` |
| `requests.py` | `requests`, `origins`, `catalog_searches` |
| `searches.py` | `slskd_searches` |
| `downloads.py` | `downloads`, `download_attempts` |
| `files.py` | `file_management`, `tagging` |
| `peers.py` | `peers` |
| `setup.py` | `setup_state` |

### `core/` (pure functions, unit-tested without fakes)
| Module | Job |
|---|---|
| `normalize.py` | names → `[track, artist, album]` arrays (rules from the normalization test) |
| `matcher.py` | is this file the requested song (title phrase, artist, exclusions, duration) |
| `strategy.py` | query shapes, usable-album check, query order |
| `ranking.py` | order helpful peers (free slot → speed) |
| `states.py` | allowed `track_state` / `file_state` transitions; one function per transition |
| `stall.py` | attempt outcomes (`noStart` 20 s, `tooSlow` < 40 kbit/s over 20 s) |

### `services/` (ABC for each one routes/workers call)
| Service | Job | Uses |
|---|---|---|
| `catalog` | find a song across MusicBrainz / ListenBrainz / Deezer; write `songs` + `song_identity` | MB, LB, Deezer clients; songs store; normalize |
| `requests` | create a request (user, song, source, origin); history (paginated) | requests, songs stores |
| `search` | run the strategy against slskd, record searches, return ranked helpful candidates; stable-count exit | slskd client; searches store; strategy, matcher, ranking |
| `download` | queue (row first), attempts, stall rule, retry on the same row, peers | slskd client; downloads, peers stores; stall, ranking |
| `files` | hand-off, verify, move, link `navidrome_id` | Navidrome client; files store |
| `tagging` | MB/LB lookup → write tags known to be true; `tagging` rows | MB, LB clients; files, songs stores |
| `auth` | login checked against Navidrome, sessions, lockout | Navidrome client; users store |
| `health` | connectivity probes | all clients |

### `workers/`
`download_monitor` (polls transfers → download service), `file_pipeline` (hand-off → verify →
tag → move → index), `cleanup` (remove handed-off transfers at slskd; today's HistoryCleaner).

### `routes/`
As today (`search`, `downloads`, `musicbrainz` → catalog, `library`, `config`, `setup`, `system`,
`events`) plus `auth`. Default-deny middleware lives next to `main.py`.

## 3. Ownership (who may change what)

| Thing | Only changed by |
|---|---|
| `track_state` `requested`/`dropped`/`reRequested` | requests service |
| `track_state` `searching`/`found`/`notFound` | search service |
| `track_state` `midDownload`/`downloaded`/`notDownloaded`/`badDownload` | download service |
| `file_state` (`downloaded` → … → `indexed`/`missing`/`deleted`) | files service (`tagged` via tagging; `deleted` after an approved delete proposal) |
| `failTag` | tagging service, in the `tagging` table only |
| `peers` | download service |
| `songs`, `song_identity` | catalog service |
| `users`, `sessions` | auth service |

Every transition goes through `core/states.py`, so an illegal jump (e.g. `requested` → `indexed`)
raises instead of being written.

**State graphs (user, 2026-10-28, #93)**: track: `requested`/`reRequested` → `searching` →
`found`/`notFound`; `found` → `midDownload` → `downloaded`/`notDownloaded`/`badDownload`
(`downloaded` added to the CHECK list: the request ends there; `badDownload` = the transfer
ended badly); `dropped` from any state before the end; every ended state → `reRequested`.
File: `downloaded` → `verified` → `tagged` → `moved` → `indexed`; `verified` → `moved` when
tagging failed; any state but `deleted` → `missing`; `moved`/`indexed`/`missing` → `deleted`;
a replacement file resets any state to `downloaded` (the hand-off trigger, in SQL).

## 4. End-to-end flow (one song)

1. **Route** `POST /catalog/.../download` → `catalog.resolve()` returns a `song_id` (creating the
   song + identities if new) → `requests.create(user, song, source, origin)`: `track_state =
   requested`.
2. **Search**: `search.run(request)` → `strategy` picks the queries → `slskd` client per query
   (stable-count exit) → `searches` store records each (`strategy` column) → `matcher` + `ranking`
   give helpful candidates → `found` / `notFound`.
3. **Download**: `download.start(request, candidates)` writes the `downloads` row, then queues at
   slskd → `midDownload`; the monitor feeds transfer states in; `stall` decides `noStart` /
   `tooSlow` → next candidate (same row, new attempt) or `notDownloaded` / `badDownload`.
4. **Hand-off**: `completed` → trigger creates the `file_management` row (`downloaded`).
5. **File pipeline**: verify → tag (MB/LB, known-true tags only) → move into the library →
   Navidrome scan → `navidrome_id` linked while the path is still known → `indexed`; or `missing`.
6. **Cleanup**: transfer removed at slskd, `downloads` row deleted; request, song and file rows
   stay.

## 5. Old → new

| Today | Becomes |
|---|---|
| `services/search.py` | split: `clients/slskd.py` (API calls) + `services/search.py` (exit rule, recording) |
| `services/query_builder.py` | `core/strategy.py` + `core/normalize.py` |
| `services/track_requester.py` | `core/matcher.py` + `core/ranking.py` + `services/search.py`; `run_ladder` removed |
| `services/search_limiter.py` | stays, used by `clients/slskd.py` |
| `services/download.py` | `clients/slskd.py` (transfers) + `services/download.py` |
| `services/download_data.py` | merged into `services/download.py` |
| `services/navidrome_library.py`, `services/library.py` | `clients/navidrome.py` + `services/files.py` |
| `services/musicbrainz_client.py` | `clients/musicbrainz.py` (mostly unchanged) |
| `services/interfaces/*` | ABCs move next to their client/service |
| `services/health.py` | `services/health.py` (uses the clients) |
| `services/bootstrap.py` | stays (startup file checks) |
| `db/*_store.py`, `db/database.py` | `stores/`; migrations 001–015 replaced by one clean initial migration |
| `workers/mb_resolver.py` | removed: its flow is §4 steps 1–3 |
| `workers/download_monitor.py` | `workers/download_monitor.py`, logic moved into the download service |
| `workers/history_cleaner.py` | `workers/cleanup.py` |
| `routes/setup.py` Navidrome calls | `clients/navidrome.py` |
| HTTP Basic auth in `main.py` | removed; `services/auth` + middleware |

## 6. Build order (bottom up; replaces the phase list in `work-plan.md`)

1. **`clients/`** + their ABCs and fakes (slskd first: fixes the first-file-only and `length`
   bugs at the source).
2. **`stores/`**: migrations 015 review + 016 (users/sessions, songs, states, attempts, tagging).
3. **`core/`**: normalize (after the normalization test), matcher, strategy (after search test
   v2), ranking, states, stall.
4. **`services/`** in flow order: catalog → requests → search → download → files → tagging;
   auth alongside stores (phase-1 decision).
5. **`workers/`**, then **`routes/`** + default-deny middleware.
6. **Validation** on the test stack (`work-plan.md` rules; `live-runs-log.md`).

## 7. Source modules: Postradio recs and Yandex import (decided 2026-10-27, not built)

Both are new ways a request **starts**; the core loop doesn't know they exist.

```
postradio recs ─┐
                ├─→ proposals (pending) ──user approves──→ requests.create() ──→ core loop
yandex sync ────┘                       └─user rejects──→ kept as rejected (never re-proposed)
```

**Rule:** a source module may only call `catalog`, `proposals` and its own client and store. It
never calls slskd, downloads or files directly.

### Shared: the approval inbox
- `proposals` table: `user_id`, `song_id`, `origin_id`, `action` (`add`, and `delete` for
  removals, see below), `status` (pending / approved / rejected), timestamps. (The old concept's
  `download_cart`, made generic.)
- `services/proposals`: list, approve, reject. Approving an `add` calls `requests.create(user,
  song, source, origin)`, the same entry point as a manual download.
- **Approval is per song, with "approve all"** (per playlist / batch). One approval page for all
  sources, filterable by source.

### Postradio
- Postradio has many features (more planned), so it gets its **own client**:
  `clients/postradio.py` (ABC + fake). Recommendations come **from Postradio**.
- `services/recs`: reads the user's taste (library via Navidrome), asks Postradio, resolves each
  track with `catalog.resolve()` → `song_id`, writes proposals. Recommendations are **not an origin** (user, 2026-10-28): no `rec_tracks`
  origin table.
  Worker `rec_puller` on a schedule.
- Other Postradio features become their own services on the same client later.

### Yandex playlists
- `clients/yandex.py` (ABC + fake). Any **public playlist** can be followed (the user's own and
  other people's are not treated differently); **several per user**. Token stored encrypted per
  user like the ListenBrainz token.
- Table **`synced_playlists`**: `id`, `user_id`, `source` (`yandex`), `source_playlist_id`,
  `name` (PV name, **renamable by the user**; a rename is pushed to Navidrome), `source_title`,
  `navidrome_playlist_id`, `source_revision`, `last_synced_at`. Playlists are kept by their id.
- Table **`playlist_items`**: `playlist_id`, `song_id`, `position`, `added_at`. PV's copy is the
  source of truth.
- **Daily sync** (config value) plus a "sync now" button. Per followed playlist: fetch; skip if the
  Yandex `revision` is unchanged; diff by Yandex track id (`song_identity` source `yandex`); new
  tracks → `catalog.resolve()` → appended to `playlist_items` + an `add` proposal. Tracks seen
  before are never proposed again, even if rejected.
- **Removed in Yandex** → removed from `playlist_items` (so from the playlist view) and a
  **`delete` proposal** offers deleting the file. Approval required. **An approved delete removes
  the file from disk** (user, 2026-10-27); Navidrome drops it on its next scan (the server runs
  `ND_SCANNER_PURGEMISSING=always`). The file row stays as history with `file_state = deleted`
  (decided 2026-10-27; `missing` = vanished on its own, `deleted` = removed on purpose). A file must not be offered for
  deletion while another playlist or another user's request still uses its song.
- **The playable playlist is a view**: a **Navidrome playlist via the API** holding only items whose
  song has an `indexed` file (`navidrome_id`), in Yandex order. `services/playlists` rewrites it
  fully after each sync and whenever a file reaches `indexed`, so it grows as approved downloads
  arrive; rejected or failed songs never appear.

### Where they sit in the layers
| Layer | Additions |
|---|---|
| `clients/` | `postradio.py`, `yandex.py` |
| `stores/` | `proposals.py`; `playlists.py` (`synced_playlists`, `playlist_items`) |
| `services/` | `proposals`, `recs`, `yandex_import`, `playlists` |
| `workers/` | `rec_puller`, `playlist_sync` (daily) |
| `routes/` | approval page, rec settings, Yandex account + followed playlists |

## 8. Custom playlists (smart / manual), one owner (planned 2026-10-27, not built)

Generalizes §7's playlist piece; nothing is manipulated in two places.

- **`synced_playlists` → `playlists`** with `kind`: `synced` (Yandex sync fills `playlist_items`),
  `smart` (a rule stored as JSON on the row), `manual` (`playlist_items` edited by hand in PV).
- Every kind answers one question, "which songs, in what order?":
  - `core/playlist_rules.py` (pure): `(rule, rows) → ordered song_ids`.
  - `services/playlists` is the **only writer to Navidrome playlists**: desired list (items, or
    evaluated rule) → keep songs with an `indexed` file → rewrite the Navidrome playlist, rename
    if needed.
- **Rule inputs**: PV stores (`songs`, `song_identity`, `requests` source/user/date/state,
  `file_management` state/dates, `tagging`, `proposals`, playlist membership); Navidrome-only
  fields (plays, rating, loved) are fetched through the Navidrome client as input data, never a
  second rule system.
- **Rebuild triggers**: after a sync, when a file reaches `indexed`, when a rule's inputs change
  (request/file state change), and daily for time-based rules.
- **Ownership**: PV owns a Navidrome playlist only if it has a `playlists` row
  (`navidrome_playlist_id`). Playlists made inside Navidrome are never touched.
- **Navidrome `.nsp` smart playlists**: possible as an *output format* for rules that only use
  fields Navidrome knows (plays, rating, loved, genre), so they update live while playing; still
  generated from the PV row, never hand-edited (the repo already has `app/assets/favorites.nsp`).

Open: whether ND-field-only rules output `.nsp` or a fixed list from PV; what happens when a
PV-owned playlist is edited directly in Navidrome (overwrite + warning, or detect drift and ask);
rule format (JSON in the shape of Navidrome's `.nsp`, or fixed presets first).

## Open
- Config layer redesign (`work-plan.md`) is not planned yet; the config object is passed into
  services until then.
