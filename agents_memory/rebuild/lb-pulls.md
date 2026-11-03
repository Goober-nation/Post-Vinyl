# ListenBrainz pulls: recommendations + radio (built and live-tested 2026-11-04, milestone 9)

Milestone 9. One **ListenBrainz** tab with
two sub-tabs, **Recommendations** and **Radio**. Both end the same way: a named **pull** of songs that
download in the background and appear in one Navidrome playlist as they arrive.

Limits (user, 2026-11-04): one user queueing at a time, one pull per user at a time. No queue
management beyond that. Pulls skip the approval inbox: the user picks the songs directly.


## What ListenBrainz returns (live, 2026-11-04)

| | Recs | Radio |
|---|---|---|
| Endpoint | `GET /1/cf/recommendation/user/{name}/recording?count=1000` | `GET /1/explore/lb-radio?prompt=artist:(NAME)&mode=easy\|medium\|hard` |
| Auth | none | user token (401 without) |
| Returns | 1000 rows: `recording_mbid`, `score`, `latest_listened_at`, plus `last_updated`, `model_id` | JSPF: 33 tracks in the one run tried, with title, creator, album, duration, MBID |
| Names | none: filled with `recording_metadata()` (one POST, up to 1000 MBIDs) | included |

- Source: LB's collaborative-filtering recommendations, read from the server source
  (`recommendations_cf_recording_api.py`). `count` is capped at 1000; `last_updated` is when LB
  stored the row. 204 = none generated yet. The docs call the endpoint experimental.
- Refresh: a community post says weekly, but neither docs nor source seen state it. Samples: one
  user's list dated 2026-10-22 (13 days old), another's 2026-08-25.
- Radio's first three tracks included a live recording and a remix, so **excluded-version
  filtering is required**. A reroll is another call. LB's level names are `easy/medium/hard`.
- Recs need the LB username. It's stored on `users` when the token is checked, so the user never types it.

## Recs process

1. **Check for new** (button): fetch the recs. A new generation for this user is stored with
   its pull date; a known one changes nothing. Needs a stored LB token.
2. Open ListenBrainz → Recs, pick a generation by date from the dropdown.
3. Set the range with a two-sided slider (from x to y, ranks 1–1000).
4. Preview: the song count and list. Optional playlist name, default `recommendations-<pull date>`.
5. **Pull**: creates the pull (refused if the user has one running).
6. Songs download in rank order, behind user requests.
7. Each song that reaches `indexed` joins the pull's Navidrome playlist.
8. **Cancel** (recs and radio): the pull becomes `cancelled`, queued songs and a request that
   hasn't reached a download yet are dropped. A download already started finishes, and its song
   joins the library and the playlist. Decided (user, 2026-11-04).

## Radio process

1. Open ListenBrainz → Radio, enter an artist and level.
2. The app fetches the radio with the user's token and previews it, excluded versions removed.
3. **Reroll** fetches again. Nothing is stored until the pull.
4. Choose start and end, optional playlist name (proposed default `radio-<artist>-<date>`).
5. Pull, then the same as recs steps 6–8. The chosen songs are stored, because the queue needs
   them; previews and rerolls are not.

## Queue (decided 2026-11-04)

Search is capped at 4 a minute, and resolving a new song costs MusicBrainz calls at 1 a second, so
1000 songs take over 4 hours. Creating 1000 `requests` up front would also make the existing
dispatch start them all at once, in front of later user requests.

So a pull's songs wait in `pull_items`, and a new **`pull_feeder`** worker moves them on:

- Every 5 s: if no request is waiting or searching (a request in `midDownload` doesn't block, so a
  slow peer queue can't stall the pull), take the next `queued` item of the oldest running pull,
  resolve it to a song, create its request, dispatch.
- A song already in the library is `reused`: no request, straight to the playlist.
- One item before a download at a time. User requests therefore always go first, without changing
  dispatch. A search already running finishes before a new user request starts.
- Cancel marks the pull cancelled and the feeder stops. Only a request not yet downloading is dropped.
- Failed songs (`notFound`, `notDownloaded`, `badDownload`) stay out of the playlist; the pull
  shows counts. A restart loses nothing, the state is in the database.

## Data (in `001_initial.sql`, user, 2026-11-04)

Four tables, following the user's original schema, plus `users.lb_username`. The merged `pulls` table and the
`source_request_id` variant were dropped: a column pointing at either of two tables can't have a
foreign key, and the app already has `origins` for this.

```sql
CREATE TABLE lb_playlists (                 -- every song of every generation, per user
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_updated        INTEGER NOT NULL,   -- ListenBrainz's generation marker
    pulled_at           INTEGER NOT NULL,   -- shown in the dropdown
    rank                INTEGER NOT NULL,   -- 1-1000
    recording_mbid      TEXT    NOT NULL,
    score               REAL    NOT NULL,
    latest_listened_at  TEXT,
    title TEXT, artist TEXT, artist_mbids TEXT, album TEXT, release_mbid TEXT, length_s REAL,
    PRIMARY KEY (user_id, last_updated, rank)
);
CREATE TABLE user_lb_playlists (
    id                     INTEGER PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                   TEXT    NOT NULL,
    lb_last_updated        INTEGER NOT NULL,  -- with user_id, finds the rows in lb_playlists
    first_rank             INTEGER NOT NULL,
    last_rank              INTEGER NOT NULL,
    state                  TEXT    NOT NULL DEFAULT 'running'
                           CHECK (state IN ('running', 'done', 'cancelled')),
    navidrome_playlist_id  TEXT,              -- set when the first song is indexed
    created_at             INTEGER NOT NULL
);
CREATE TABLE user_radio_requests (
    id                     INTEGER PRIMARY KEY REFERENCES origins(id) ON DELETE CASCADE,
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                   TEXT    NOT NULL,
    artist                 TEXT    NOT NULL,
    level                  TEXT    NOT NULL CHECK (level IN ('easy', 'medium', 'hard')),
    size                   INTEGER NOT NULL,
    state                  TEXT    NOT NULL DEFAULT 'running'
                           CHECK (state IN ('running', 'done', 'cancelled')),
    navidrome_playlist_id  TEXT,
    created_at             INTEGER NOT NULL
);
CREATE TABLE pull_items (                   -- the chosen songs and the shared queue
    origin_id       INTEGER NOT NULL REFERENCES origins(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    recording_mbid  TEXT    NOT NULL,
    title           TEXT, artist TEXT,
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued', 'requested', 'reused', 'failed', 'cancelled')),
    request_id      INTEGER REFERENCES requests(id),
    song_id         INTEGER REFERENCES songs(id),
    in_playlist     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (origin_id, position)
);
```

How it connects:

- **Origin**: a pull inserts `origins` (kind `lb_recs` or `lb_radio`) and reuses that id, like
  `catalog_searches`. The kind says which table to read; requests get that `origin_id`.
- **Cost of two tables**: state, playlist id and cancel are handled for each, and "one running
  pull per user" is checked in the service, as no single index covers both tables.
- **New ways to pull** (user, 2026-11-04: the list will grow): one detail table, one `origins`
  kind and one code source producing the items. `pull_items`, the feeder, cancel and playlists stay
  as they are, reading state and playlist id through a small per-kind lookup.
- **Progress**: `pull_items.request_id` → `requests.track_state`. No new request or file states.
- **Done**: the feeder ends a pull when no item is `queued` and every requested item has ended.
- **Playlist**: `services/playlists` keeps the items whose song has an `indexed` file, in
  `position` order, and rewrites the Navidrome playlist. A playlist removed in Navidrome is recreated.
- **Covers**: per page in the tab, through the catalog cover route (`release_mbid`).
- **Library match**: before requesting, the feeder looks up `song_identity` (`musicbrainz`,
  `recording_mbid`); a song with an `indexed` file becomes `reused`. Decided (user, 2026-11-04).

## Layers

| Layer | Change |
|---|---|
| `clients/listenbrainz.py` | `recommendations(name)`, `lb_radio(token, prompt, mode)` (+ fake) |
| `clients/navidrome.py` | `save_playlist`: create or replace (+ fake; source checked at 0.64.2) |
| `core/` | excluded-version filter on titles (reuse the matcher's rules); default names |
| `stores/pulls.py` | the tables above; `RequestsStore.origin_user` learns pulls |
| `services/pulls.py` | check for new, preview, pull, cancel, status, feed; ABC |
| `services/playlists.py` | only writer to Navidrome playlists: indexed songs of the pull, in position order, full rewrite. Milestones 10–11 extend it |
| `services/requests.py` | `lb_recs`, `lb_radio` join `REQUEST_SOURCES` |
| `workers/` | `pull-feeder` and `pull-playlists`, every 5 s; items carry `in_playlist`, so no pipeline trigger is needed |
| `routes/pulls.py` | `POST /api/lb/recs/check`, `GET /api/lb/recs`, `GET /api/lb/recs/{id}/tracks`, `GET /api/lb/radio`, `POST`/`GET /api/pulls`, `POST /api/pulls/{id}/cancel` |
| `web/src/listenbrainz.ts` | the tab, on `rebuild/frontend` (not merged into `rebuild/base` yet) |

Playlists are written with the user's own Navidrome session, so they belong to that user.
