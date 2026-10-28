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
| `track_state` `midDownload`/`notDownloaded`/`badDownload` | download service |
| `file_state` (`downloaded` → … → `indexed`/`missing`/`deleted`) | files service (`tagged` via tagging; `deleted` after an approved delete proposal) |
| `failTag` | tagging service, in the `tagging` table only |
| `peers` | download service |
| `songs`, `song_identity` | catalog service |
| `users`, `sessions` | auth service |

Every transition goes through `core/states.py`, so an illegal jump (e.g. `requested` → `indexed`)
raises instead of being written.

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
