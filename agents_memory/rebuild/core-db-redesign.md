# Core DB redesign: schema & decisions

Working note for the post-beets, post-recs database redesign (Core Rebuild board). Edit in place as
columns or decisions change; don't append a changelog.

**Status (2026-10-28):** the schema is built as `app/stores/migrations/001_initial.sql` (#84),
run by `app/stores/database.py`. The SQL sections further down (015, "planned") are history:
where they differ from `001_initial.sql` and the decisions below, the migration wins.

## Stores (#86, decided 2026-10-28)

- One plain class per table group in `app/stores/` (users, songs, requests, downloads, slskd,
  files, setup). No ABCs or fakes: tests use a real SQLite file.
- **The caller passes the connection** (user): every method takes `conn`, so a service combines
  stores in one `Database.transaction()` (all or nothing). Stores hold no connection.
- Methods return **frozen dataclasses**, never `sqlite3.Row`. Times are passed in as `now`.
- History is **page-number pagination** (user): `LIMIT/OFFSET` ordered `last_used_at DESC, id
  DESC`. A row bumped mid-browse can shift between pages; that is accepted.
- Typed Soulseek queries go only through `record_slskd_query` (an upsert on the partial unique
  index); `create()` refuses `source='slskd'`, which enforces "no origin" outside the DB.
- `downloads.complete()` is the only way to reach `completed` (it sets the location, and the
  trigger hands the file over). `songs.find_or_create` raises `IdentityConflict` when ids point
  at two songs; the service decides.
- `requests.user_id` doesn't cascade: deleting a user who has requests fails (history is
  never pruned). Revisit if user deletion is ever needed.

## Remember later (from building #84, 2026-10-28)
- **Migrations**: `migrations/NNN_name.sql`, numbered without gaps; the applied number lives in
  `PRAGMA user_version`. **Never edit an applied migration**: every change is a new file. Each
  runs in one transaction with its version bump. An app older than the DB refuses to start.
- **Needs SQLite ≥ 3.38** (`unixepoch()`; upsert inside a trigger). The test container has 3.46.
- **Connections**: one per thread (`Database.connect()`), foreign keys on, WAL. Stores must not
  share a connection across threads.
- **Hand-off only fires when the request has a `song_id`**: services must set the song before a
  download completes (Soulseek tab: matched song, or a `[u]` song from the file's data).
- **A re-download resets the song's file row** (state `downloaded`, `verification` and
  `navidrome_id` cleared): the files service must expect an indexed file to come back and re-link
  the new `navidrome_id`. The old file on disk is the files service's to remove.
- **`file_location` is relative to the source's download dir**; after the move Navidrome owns the
  path (only `navidrome_id` is kept).
- **Deletes that are refused on purpose**: a song with a file (no cascade from `songs`), and a user
  with requests (requests are never pruned). Sessions cascade with the user.
- **Rules enforced by store tests, not the DB**: a typed Soulseek request has no origin (#86).
- **`user_settings`** (user, 2026-10-28): key/value per user `(user_id, key, value JSON,
  updated_at)`, cascading with the user. For loose preferences only; anything that needs
  encryption or constraints gets a real column on `users` (like `lb_token_enc`).
- **Lockout state is in memory** (auth service), so it resets on restart.
- Times are unix seconds everywhere; every FK column is indexed (a test checks this).

## Decided 2026-10-28 (milestone 4 planning; wins over everything below)
- **One clean initial migration** with the full schema (architecture.md); 015/016 are not
  kept as separate files. `stores/database.py` runs it and sets `PRAGMA foreign_keys = ON`.
- **Download sources are swappable** (architecture.md, "Swappable sources"), so:
  - **`downloads` is generic**: one row per "get this file" with `request_id`, `source`
    (`slskd`, …), a generic `state` (queued | downloading | completed | failed | cancelled),
    timestamps and the finished file's location. Core and the hand-off trigger see only this.
  - **Each source keeps its own prefixed tables**: `slskd_searches` → `requests` (a search
    runs before any download row exists; keeps `strategy`), `slskd_attempts` → `downloads`
    (peer, remote file, transfer id, rank, outcome, timings; replaces `download_attempts`),
    `slskd_peers` with **no FK** (a peer's record spans downloads; replaces `peers`).
  - **The hand-off trigger copies the generic location** from `downloads` into
    `file_management`; nothing on the file side reads slskd fields. A source may deliver
    files that skip a file state where `core/states.py` allows it.
- `users` has **no `failed_logins` / `locked_until`**: the lockout is in memory in the auth
  service (user, 2026-10-28).
- **Soulseek-tab downloads always get a song** (user, 2026-10-28): when a file is queued, the
  catalog service tries to match it; a match links the song and its identities. **No match**: a
  song row is made from the file's own data (tags, else path) with **no `song_identity` rows**
  (that is what "unknown" means; no flag column). Its own tags are kept (no catalog tags), and it
  moves under one top-level **`[u]/Artist/Album/`** folder. It can be re-matched later: add the
  identities, then move, scan, then retag (Navidrome keeps the id through a move or a retag
  alone, not both between scans).
- **`CHECK (source <> 'slskd' OR origin_id IS NULL)` dropped** (user, 2026-10-28): it guarded
  one code path and hard-coded a source name; a requests-store test covers the rule instead.
- **Recommendations are not an origin** (user, 2026-10-28): no `rec` origin kind, no
  `rec_tracks` table.
- Superseded by the above: `downloads.username/filename/slskd_id/search_id`,
  `download_attempts`, `peers` as named, and `file_management.username/filename`.

## Schema

### Built: migration 015 (`app/db/migrations/015_core_redesign.sql`)

```sql
-- where a request came from (optional); type tables share the origins id
CREATE TABLE origins (
    id    INTEGER PRIMARY KEY,
    kind  TEXT NOT NULL                -- catalog_search | rec | …
);
CREATE TABLE catalog_searches (       -- typed MusicBrainz-tab history
    id            INTEGER PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
    query         TEXT    NOT NULL,
    kind          TEXT    NOT NULL,    -- recording | album | artist
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_catalog_searches_query ON catalog_searches(lower(query), kind);

-- one row per track asked for, whatever started it
CREATE TABLE requests (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,    -- slskd | catalog | rec | …
    origin_id     INTEGER REFERENCES origins(id) ON DELETE SET NULL,
    query         TEXT,                -- typed query, or "artist – title"
    mbid          TEXT,                -- recording MBID for catalog/rec
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL,    -- bumped when a typed slskd query repeats
    CHECK (source <> 'slskd' OR origin_id IS NULL)
);
CREATE INDEX idx_requests_origin    ON requests(origin_id);
CREATE INDEX idx_requests_last_used ON requests(last_used_at);
CREATE UNIQUE INDEX idx_requests_slskd_query ON requests(lower(query)) WHERE source = 'slskd';

-- one row per actual slskd search (a query strategy = several rows per request)
CREATE TABLE slskd_searches (
    id          TEXT    PRIMARY KEY,   -- slskd's search id, always real (never cache-*)
    request_id  INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    query       TEXT    NOT NULL,      -- exact text sent to slskd
    artist      TEXT,                  -- client-side filter
    created_at  INTEGER NOT NULL
);
CREATE INDEX idx_slskd_searches_request ON slskd_searches(request_id);

-- the network transfer only; one row per requested file, kept through every retry
CREATE TABLE downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- ours, never changes
    request_id    INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    search_id     TEXT    NOT NULL REFERENCES slskd_searches(id) ON DELETE CASCADE,
    slskd_id      TEXT,                -- current attempt's transfer id, informational
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

-- every peer tried for a download; first row = original request; retries = COUNT(*) - 1
CREATE TABLE download_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id   INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    username      TEXT    NOT NULL,    -- peer selection skips every username listed
    attempted_at  INTEGER NOT NULL,
    success       BOOLEAN NOT NULL DEFAULT 0   -- slskd accepted the queue request
);
CREATE INDEX idx_download_attempts ON download_attempts(download_id, username);

-- what happened to the file after it arrived; created by trigger, outlives downloads
CREATE TABLE file_management (
    id             INTEGER PRIMARY KEY,
    source         TEXT    NOT NULL,   -- copied from requests by the trigger
    username       TEXT    NOT NULL,   -- with filename, locates the file under
    filename       TEXT    NOT NULL,   --   downloads/complete/soulseek/<username>/
    stage          TEXT    NOT NULL,   -- pending | moved | missing | unindexed
    placed_path    TEXT,               -- relative to the library root, incl. final filename
    navidrome_id   TEXT,               -- Navidrome media_file.id once indexed
    downloaded_at  INTEGER NOT NULL
);
CREATE INDEX idx_file_management_stage     ON file_management(stage);
CREATE INDEX idx_file_management_navidrome ON file_management(navidrome_id);

CREATE TABLE mb_provenance (           -- 1:1 satellite, never a column on file_management
    file_management_id  INTEGER PRIMARY KEY REFERENCES file_management(id) ON DELETE CASCADE,
    mb_recording_id     TEXT NOT NULL
);

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

-- only peers with a current bad record; no row = clean peer
CREATE TABLE peers (
    username       TEXT    PRIMARY KEY,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    blocked_at     INTEGER             -- NULL = not blocked
);
```

Unchanged from before 015 (onboarding flags, out of scope):
`setup_state(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`.

### Planned: migration 016, users & sessions (decided 2026-10-25, not written)

```sql
-- every PV user is a Navidrome user; row created on their first successful PV login
CREATE TABLE users (
    id               INTEGER PRIMARY KEY,
    username         TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- = Navidrome username
    nd_password_enc  TEXT    NOT NULL,     -- Fernet (key PV_SECRET_KEY in .env); refreshed every login
    lb_token_enc     TEXT,                 -- Fernet; NULL until the user enters it in the app
    is_admin         BOOLEAN NOT NULL DEFAULT 0,  -- copied from Navidrome
    status           TEXT    NOT NULL DEFAULT 'active',  -- active | needs_reauth
    failed_logins    INTEGER NOT NULL DEFAULT 0,  -- 10 fails → locked 5 min
    locked_until     INTEGER,
    created_at       INTEGER NOT NULL
);

-- in-app login sessions, 30-day sliding expiry
CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT    NOT NULL UNIQUE,  -- SHA-256 of the cookie token; raw token never stored
    created_at    INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL          -- pushed forward on each use
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
```

Losing `PV_SECRET_KEY` loses every stored Navidrome password and ListenBrainz token: back the key
up outside the database. Users and sessions replace HTTP Basic auth (removed in the same change).

### Planned: songs, states, richer attempts, tagging (decided 2026-10-25, not written)

Table names from 015 stay (`requests`, `slskd_searches`, `downloads`, `file_management`, …).

```sql
-- every song requested through Post-Vinyl; metadata only, shared by all users
CREATE TABLE songs (
    id            INTEGER PRIMARY KEY,
    title         TEXT    NOT NULL,
    artist_name   TEXT    NOT NULL,
    album_title   TEXT,
    track_number  INTEGER,
    disc_number   INTEGER CHECK (disc_number >= 1),   -- NULL = no disc
    disc_count    INTEGER,                            -- disc folder only if > 1
    duration_s    INTEGER,
    edition       TEXT    CHECK (edition IN ('standard', 'deluxe'))
);

-- source ids only: 'mb' (recording MBID), 'deezer', 'isrc' (hint, not unique per song), …
CREATE TABLE song_identity (
    song_id    INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    source     TEXT    NOT NULL,
    source_id  TEXT    NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX idx_song_identity_song ON song_identity(song_id);

-- changes to 015 tables
ALTER requests:        + song_id INTEGER REFERENCES songs(id)
                       + track_state TEXT NOT NULL CHECK (track_state IN ('requested','dropped',
                           'reRequested','searching','found','notFound','midDownload',
                           'notDownloaded','badDownload'))
                       - mbid   (→ song_identity)
ALTER file_management: + song_id INTEGER NOT NULL UNIQUE REFERENCES songs(id)  -- one current file per song
                       + file_state (replaces stage) CHECK (file_state IN ('downloaded','verified',
                           'tagged','moved','indexed','missing','deleted'))
                       + verification TEXT            -- 'success' or an error code
                       - source (copy no longer needed; see open questions)
ALTER download_attempts: + rank INTEGER, + filename TEXT (remote path), + slskd_transfer_id TEXT,
                         + outcome CHECK (outcome IN ('completed','noStart','tooSlow','failed','skipped','interrupted')),
                         + started_at / ended_at (attempted_at → started_at); - success
DROP  mb_provenance    (the MBID lives in song_identity; file → song_id → song_identity)

-- one row per tagging attempt; failTag lives here, never in a state column
CREATE TABLE tagging (
    id               INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES file_management(id) ON DELETE CASCADE,
    status           TEXT    NOT NULL CHECK (status IN ('tagged', 'failTag')),
    error            TEXT,
    mb_recording_id  TEXT,
    mb_release_id    TEXT,
    tags_written     TEXT,                 -- JSON
    tagged_at        INTEGER NOT NULL
);
```

- **States** (user): `track_state` covers the request until the download finishes; `file_state`
  covers the file afterwards. Both are columns; each state has one owner, so they can't drift.
  `failTag` lives only in `tagging`.
- **One current file per song**: a replacement (e.g. after `badDownload`, or a better copy)
  updates the file row, never adds a second. `navidrome_id` stays on the file row (it changes when
  the file is replaced), not in `song_identity`.
- **Songs are tables in the same SQLite file** (FKs only work within one file). Songs land there
  when requested; downloads, files and later recs reference them, so no columns are copied.
- **Tagging**: no beets, no Picard. Look up MusicBrainz/ListenBrainz with the song's data, or use
  the MBID from `song_identity` when the user's search already provided it; write only tags known
  to be true.
- `songs.requested_at` from the old concept is dropped: request data, belongs on `requests`.
- **Decided 2026-10-26:**
  - `file_state` has **no `unindexed`**: a moved file stays `moved` until Navidrome lists it.
  - **The placed path is not saved** (no `placed_path` column, no `pending_imports` table).
    Navidrome owns paths; the file row links through `navidrome_id`. Consequence to handle: the
    path must be used to find the `navidrome_id` while the app still knows it (right after the
    move); after a restart it has to be found another way (e.g. by song metadata).
  - `slskd_searches` gets a **`strategy`** column (L0, T, AT, album-name, single, self-titled…).
  - **Requests are never pruned**, so `source` stays reachable through the request; the copy on
    `file_management` is dropped. This reverses the 2026-10-03 pruning decision (automatic age
    pruning and the "clear history" UI). Recent searches (#13): no delete for now; the list is
    paginated through an index so loading time doesn't grow (decided 2026-10-26).

**Decided 2026-10-25:** `requests` gets `user_id INTEGER NOT NULL REFERENCES users(id)` (per-user
history and downloads). Users & sessions are built in **phase 1 (DB)**, so the FK is real from the start.

---

## Settled decisions so far

- **`search_id` is always a real slskd id.** At insert time, a candidate that came from a
  query-cache hit gets resolved to its underlying real `search_id` before landing on the `downloads`
  row — `cache-*` ids are never stored there. This is what kills issues #59/#60 at the source instead
  of patching retry to special-case them.
- **Search-result reuse on retry is being kept**, not dropped — retry re-reads the stored
  `search_id`'s existing results rather than starting a new Soulseek search. This is the whole reason
  `search_id` has to be on the row at all.
- **`retry_count` is dropped** (2026-10-02, replaces "stays a real ceiling"). Every peer, including
  the original request, gets a `download_attempts` row the moment it's queued, so the retry count is
  `COUNT(*) - 1` for that `download_id`; the `max_retries_per_track` ceiling is checked against it.
  Peer exhaustion is still expected to be the normal way a chain ends.
- **`downloads` is split into two tables** rather than given one unified lifecycle column:
  `downloads` tracks only the network transfer (`queued → downloading → completed/failed/cancelled`);
  `file_management` owns everything about what happens to the file
  *after* — starts the moment a download completes, ends at moved/missing. This is the direct answer
  to "a universal way of labeling files starting from its state after the download": that table is
  it. Root-cause fix for why `downloads` accreted `file_moved`/`import_skipped`/`import_unmatched`/
  `target_dir` in the first place — those are all post-completion file concerns that had no table of
  their own before.
- **One `source` enum replaces `is_rec_download` + `is_library_download`.** Those two booleans encode
  a 3-way category (`(0,0)`=slskd, `(0,1)`=MB, `(1,0)`=rec, `(1,1)`=nothing/undefined) — exactly the
  kind of implicit encoding that causes bugs. Values, matching the actual UI tabs rather than backend
  service names: **`slskd`** (direct Soulseek-tab search+queue), **`catalog`** (the MusicBrainz /
  online-catalog tab; was `search` in earlier drafts), **`rec`** (recommendation pull — kept as a valid
  value even though the rec pipeline is currently archived, not gutted, so it doesn't need another
  migration if that comes back). Lives on **`requests`** (2026-10-03 —
  it describes why something was requested); `downloads` reads it via `request_id`, and the handoff
  trigger copies it onto `file_management`.
- **`downloads` gets its own self-generated, never-mutated primary key**, instead of borrowing slskd's
  transfer id. Today `id` starts as a synthetic `pending:{username}:{filename}:{timestamp}` string and
  gets **rewritten in place** to slskd's UUID once adopted (`UPDATE downloads SET id = ? ... WHERE id
  = ?`) — the row's identity changes mid-life and is borrowed from a system we don't control. Fix: an
  autoincrement (or app-generated) id that never changes for the row's whole life; `slskd_id` stays as
  a separate nullable column, filled in once adopted, purely informational — never a primary key.
  Everything that references a download (placements, retry attempts) points at our own stable id.
- **File identity and download identity are two different things — resolved by scope, not by a shared
  table.** A download row only exists for files this app pulled down; files can exist in the library
  that never went through a download here (pre-existing content, anything added another way). The
  placement table only tracks files *this service placed* (scoped to its own downloads, exactly like
  `downloads` itself is scoped to its own transfers) — that is not duplicating Navidrome, it's this
  app's bookkeeping of its own actions. What would duplicate Navidrome is trying to build a general
  file catalog covering the whole library — not doing that. If a placement needs a pointer to "the
  actual Navidrome-side song," that's one optional column, `navidrome_id`, filled in lazily (e.g. once
  a scan confirms the file's indexed, resolved via Navidrome's API by path/filename) — a thin
  cross-reference, never a mirrored table. Navidrome stays the one source of truth for what files
  exist and where.
- **`progress`/`speed` are dropped entirely, not just from `downloads` design — they never belonged in
  the DB.** Checked: `GET /api/transfers` (what the frontend actually polls) calls
  `download_service.get_status()`, which hits slskd's live API directly — it never reads these columns
  from the DB. Grepped the whole codebase for any read of `downloads.progress`/`downloads.speed`: zero.
  Write-only, exactly like `peers.last_seen` was. No use keeping them.
- **`downloads.created_at` keeps its name** (reversing the earlier `started_at` rename). Checked
  `upsert_transfer`: it's set once at the row's first insert and never touched by any retry/update path
  — it genuinely marks when the whole retry chain began, not any single attempt's start. `created_at`
  is the more accurate name for that; `started_at` risked implying "when the transfer itself started
  downloading," which conflicts with the row already existing in `queued` state before that.
- **`source` lives on `requests` and is copied to `file_management`** (revised 2026-10-03; was
  "`downloads` + `file_management`"). Active-transfer logic that needs it (e.g.
  `download.manual_gate_minutes` telling manual from automatic downloads) joins
  `downloads.request_id → requests.source`. `file_management` keeps its own copy because it outlives
  both `downloads` and possibly the request. Set once by the trigger, never independently mutated.
- `peers.last_seen` is dropped — not mandatory, confusing (write-only, nothing reads it, and the name
  doesn't even mean what it sounds like).
- **Placement table is named `file_management`**, and its row is created the instant a download hits
  `completed` (not lazily on first placement attempt).
- **SQL does the handoff, not app code** (revised 2026-10-02). An `AFTER UPDATE OF state` trigger on
  `downloads` creates the `file_management` row the moment `state` becomes `completed`, copying
  `username`/`filename` from the download and `source` (+ `mbid` into `mb_provenance`) from its
  request. Once the file is
  handled, the `downloads` row is deleted **and** the transfer is removed from slskd
  (`?remove=true`) — otherwise `upsert_transfer` re-adopts slskd's still-listed transfer, re-creates
  the row and fires the trigger again. Nothing on `file_management` points back at `downloads`;
  `download_id` is gone.
  - `Database.connect()` sets `PRAGMA foreign_keys = ON` (was missing before 015; FKs were decorative).
- **The app only tracks transfers it started itself** (2026-10-02). `upsert_transfer`'s path (c),
  which adopts unknown slskd transfers as new rows, is removed; an unmatched slskd transfer is
  ignored. Prerequisite: every queue call site writes its `downloads` row (with `request_id` +
  `search_id`) **before** calling slskd, as the monitor's retry already does. Rows are therefore always
  inserted as `queued`, so the single `AFTER UPDATE` handoff trigger covers every completion — no
  INSERT trigger needed.
- **One retry implementation: the monitor's DB-backed `_attempt_retry`** (2026-10-02). The old
  in-memory `SlskdDownload.retry()` (`_tried_peers`/`_retry_counts`) is removed; the UI Retry button
  (`POST /api/queue/retry/{id}`) calls the monitor's retry instead, bypassing the
  `auto_retry_manual_soulseek` gate the way the button does today. Expected to close #59.
- **One `downloads` row per requested file, kept through every retry** (2026-10-02). Today each
  retry inserts a *new* row and abandons the old one, and `_pick_alternative_peer` only skips the
  most recent peer, so a chain can go A → B → A. New rule: a retry updates the same row (new
  `username`/`filename`/`slskd_id`, `state` back to `queued`), and `download_attempts` (renamed from
  `download_retry_attempts`) becomes that row's tried-peer list — peer selection skips every peer in it. Attempts are deleted with
  the row (`ON DELETE CASCADE`) once the download has been handed off.
- **File identity = Navidrome's `media_file.id`** (live-tested 2026-10-02, see
  `../topics/navidrome-api.md`): it survives a move or a retag, unlike `pid`. Everything about the file
  (tags, current path, audio properties) is read from Navidrome on demand, never copied here.
  `placed_path` is kept as history after `navidrome_id` is known — a record of where we put the file,
  not where it is now.
- **Paths are relative to the library root**, never absolute, so a config change to the root doesn't
  invalidate old rows.
- **`file_management.created_at` renamed to `downloaded_at`** — more precise, since the row is created
  exactly at the moment its download completes.
- **MB provenance moves to its own satellite table (`mb_provenance`), not a column on
  `file_management`.** Same lesson as why `downloads` became a god-table in the first place: unrelated
  concerns kept getting bolted onto one table. `mb_recording_id` is known at request time, so it
  lives on `requests.mbid` (2026-10-03; was `downloads.mb_recording_id`) — but for long-term/post-prune survival it's mirrored via trigger into
  `mb_provenance`, keyed 1:1 to `file_management.id` (not `download_id`, so it survives `downloads`
  pruning exactly like `file_management` itself does). `file_management`'s own columns stay
  permanently placement-only — no MB-specific column ever lands there. If rec comes back later, it
  gets its own `rec_provenance` table on the identical pattern, not another bolt-on.


## Delivery (decided 2026-10-03)

- **Existing overlap to resolve in the downloads step:** `app/workers/history_cleaner.py` already
  removes terminal transfers from slskd on a timer (`?remove=true`) while deliberately *keeping*
  local rows, and skips completed transfers whose file hasn't moved yet. Under the new design the
  handoff trigger moves the file's bookkeeping into `file_management` at completion, so the
  download row and its slskd record can both go right away; HistoryCleaner should become the
  "delete handed-off downloads at slskd + locally" pass (or be merged into it).

- **Start fresh**: migration 015 drops the old tables and creates the new schema empty; no data is
  carried over (pre-013/014 backup: `musica_pre_013_014_backup.db`, untracked).
- **One PR** for the whole redesign, tracked by a single GitHub issue with a checklist.
- Migration 014 committed on its own (`248a979`); the #60 stopgap in `mb_resolver.py` was dropped —
  the redesign removes `cache-*` ids at the source.

---

## `downloads` notes

`state` stays a simple transfer state now — `downloaded`/`moved`/`missing` don't belong here anymore,
they're the new placement table's job.

Moved to `requests`: `source` (replaces `is_rec_download` + `is_library_download`) and
`mb_recording_id` (as `requests.mbid`).

Cut, no replacement:, `playlist_id`
(only the archived playlist services wrote it), `import_unmatched` (was beets tag-match, no
equivalent now), `progress`/`speed` (write-only, frontend gets these live from slskd, never from the
DB — see settled decisions above).

Moved to `file_management` (post-completion file concerns): `target_dir` (as `placed_path`),
`file_moved` + `import_skipped` (as `stage`).

## `file_management` notes

One row per file this app downloaded. Created by the `downloads_completed_handoff` trigger, never by
app code; outlives its `downloads` row.

`stage` values (unchanged for now):
- `pending` — download completed, file not placed yet
- `moved` — moved to `placed_path`; terminal success
- `missing` — file never appeared on disk within the timeout; terminal failure
- `unindexed` — moved, but a Navidrome scan that finished *after* the move still doesn't list it
  (rejected / not audio / deleted); terminal, logged

**Linking `navidrome_id`** (planned 2026-10-03): each monitor poll looks up `stage='moved' AND
navidrome_id IS NULL` rows via native `GET /api/song?path=<placed_path>`. That filter is a
**substring** match (live-tested), so the returned `path` must be compared exactly. A miss after a
scan that completed after the move (`getScanStatus`) → `unindexed`. Requires Navidrome's music
folder == the app's library root; a mismatch must be a loud, unmissable error: decided 2026-10-03 — a mismatch confirmed at startup
(Navidrome reachable, its sampled song paths don't exist under the app's library root) **refuses to
start**; one detected later (e.g. empty library at startup, first file goes `unindexed`) shows a
persistent red frontend banner. Navidrome unreachable at startup = warning only.

Dropped from the earlier draft: `download_id` (handoff makes it unnecessary), `target_dir`
(replaced by `placed_path`, which includes the filename), `resolved_at` (nothing would read it).

## `mb_provenance` notes

Populated by the same trigger as `source` (see above) — app code never writes this table directly.
Keyed to `file_management.id`, not `download_id`, so it survives `downloads` pruning the same way
`file_management` itself does.

## Requests and searches notes (2026-10-03)

Replaces the single `searches` table. Shape: origin tables (where a request came from) → one
generic `requests` hub → one `slskd_searches` row per actual slskd search → `downloads`.

Decided:
- **`requests` is the hub for every "get me this"**, whatever started it. **One request per track**
  — an album click creates one request per track — so there is no `kind` column.
- **`slskd_searches` gets a new row for every slskd search**, linked by a real FK `request_id`.
  A query ladder = several rows for one request.
- **`downloads` links to its request directly** (`downloads.request_id`), not only via
  `search_id`: a download row exists before its search result is final, and a retry changes it.
- **Back-link to the origin = a shared `origins` table with real FKs** (2026-10-03; replaces the
  earlier polymorphic `source` + `origin_id` design and its per-origin triggers). Every origin of
  any type gets one row in `origins(id, kind)`; type-specific tables (`catalog_searches`,
  `rec_tracks`, …) reuse that id as their own primary key (`REFERENCES origins(id) ON DELETE
  CASCADE`). `requests.origin_id REFERENCES origins(id) ON DELETE SET NULL`. Result: a bad id is
  rejected by the FK itself, and deleting an origin deletes its type row and nulls the request's
  link — no triggers. Live-tested in SQLite. Rules: creating an origin = two inserts (origins, then
  the type row with the same id) in one transaction; **deletes go through `origins`**, never the
  type table directly (that would leave an orphan `origins` row). A new origin = a new `kind` value,
  plus a type table only if it has columns to store. `origin_id` is optional: typed slskd searches
  and catalog **browsing** (artist → album → track, no typed search) leave it NULL — an `origins`
  row holding only `kind='catalog_browse'` would say nothing `source='catalog'` doesn't already. If
  browse details ever matter, add `catalog_browses` then. Sentinel ids (e.g. 0 for "none") rejected:
  NULL already means none, and a real FK would need a dummy row.
- **Query-cache hits reuse the original real slskd `search_id`** — no `cache-*` id is ever minted,
  stored, or handed downstream.
  **The cache only reuses searches within the same request** (2026-10-03): every slskd search
  belongs to exactly one request, so a cross-request hit (e.g. the same catalog track asked for
  twice within the cache TTL) runs a fresh search instead. Reason: with one owner per search,
  pruning the owner would otherwise cascade into another request's downloads. Typed Soulseek
  repeats already collapse into one request, so they still hit the cache. If cross-request reuse
  ever matters (slskd rate limits), add a `request_searches` many-to-many link table then.
- **Repeat queries collapse to one row with a bumped `last_used_at`** — on `catalog_searches` for
  catalog history, on `requests` for typed slskd searches.
- A typed Soulseek-tab search needs no origin table: the request itself (`source='slskd'`,
  `origin_id` NULL) is the history entry.

- **Requests can be pruned; everything hanging off one goes with it** (2026-10-03):
  `slskd_searches.request_id`, `downloads.request_id` and `downloads.search_id` are all
  `ON DELETE CASCADE`, and `download_attempts` already cascades from `downloads`. `file_management`
  and `mb_provenance` have no FK to any of these, so a placed file's record survives. Pruning must
  skip requests with an active download (or cancel it at slskd first) — otherwise the slskd transfer
  keeps running with no row, and is ignored because the app no longer adopts unknown transfers.
  Pruning runs **both ways** (2026-10-03): automatically, deleting requests older than a
  configurable age **in days**, and manually from the UI (ties in with #13). Both use the same
  `DELETE FROM requests WHERE … AND NOT EXISTS (active download)` and let the cascades do the rest. Defaults: automatic age **30 days, `0` = off**; the UI
  offers a delete button on each recent-search entry (#13) **and** a "clear history" action.
  The age setting lives in the downloads config category (2026-10-03).

All FK columns are indexed (see schema): SQLite does not index FK columns automatically,
and without them every cascade delete scans the child table.

- **No search status or counts in the DB** (2026-10-03): `status`, `response_count`, `file_count`
  are dropped. slskd is the source of truth; live state is short-lived and stays in memory. The
  1-second progress poll already goes memory → slskd and never touched the DB (checked:
  `SlskdSearch.get_progress`). The counts were write-only. The recent-searches list gets each
  entry's state from the in-memory job, or from slskd by id (`GET /api/v0/searches/{id}`) when
  the app has restarted — which also removes the stuck-`searching`-after-restart bug.

## `peers` notes

Only peers with a *current* bad record. No row = clean peer.

Decided 2026-10-03:
- **`is_blocked` dropped** — it duplicated `blocked_at` (every path set/cleared both together).
  Blocked ⇔ `blocked_at IS NOT NULL`.
- **Unblocking deletes the row** (ban expiry after `peer_ban_days`, checked lazily on lookup, and
  the "Unblock all peers" button) instead of resetting it to `failure_count = 0` — a reset row was
  identical to no row, and the table grew with every peer that ever failed.
- **Peer selection checks only `blocked_at` (not expired)**, not "blocked OR failure_count >=
  threshold": reaching the threshold always sets `blocked_at`. Accepted side effect: lowering
  `bad_peer_threshold` doesn't retroactively block peers already over the new value until they
  fail again.
- `last_seen` dropped — write-only, and held the time of the last *failure* despite the name.
