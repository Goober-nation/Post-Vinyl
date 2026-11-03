# Post-Vinyl API

The backend the frontend is built on (milestone 7, stage 1). Every endpoint is listed here;
`tests/test_routes/test_api_doc.py` fails when one isn't. With `PV_DEV=1` the app also serves
the generated reference at `/docs` and `/openapi.json` (exact request and response models).

## The rules every call follows

- **Login**: `POST /api/auth/login` with a Navidrome username and password sets the
  `pv_session` cookie (HttpOnly, Secure, SameSite=Lax, 30 days, sliding). Everything except
  login and `GET /api/system/ping` needs it: no cookie → `401`.
- **Same origin**: every `POST`, `PUT`, `PATCH` and `DELETE` needs an `Origin` (or, failing that,
  `Referer`) header equal to the app's public origin, else `403`.
- **Admins** are Navidrome admins. An admin-only call by anyone else → `403`.
- **A user only sees their own** requests, searches and downloads; someone else's → `404`.
- **Errors** answer `{"detail": "..."}`:

  | Status | Means |
  |---|---|
  | `401` | not logged in |
  | `403` | admins only, or the Origin check failed |
  | `404` | no such thing, or not yours |
  | `409` | not possible right now: the request moved on, the queue is paused, or **a source this needs is turned off** (nothing is sent to it) |
  | `422` | the input doesn't fit (empty search, a value outside its range…) |
  | `429` | too many failed logins (`Retry-After` says how long) |
  | `502` | a catalog source failed while browsing |
  | `503` | a service isn't reachable or set up (no catalog answered, ListenBrainz can't check a token, setup not run) |

- **Sources**: every call to an outside service (MusicBrainz, Deezer, ListenBrainz, slskd) only
  happens while that source is on for the user the work is for: on in the install's config and
  not switched off in the user's settings.
- **Polling**: the slskd and downloads tabs poll while open (1–2 s). Successful polls of
  `/api/downloads`, `/api/slskd/searches` and `/api/requests` stay out of the access log.
- **No file paths** are returned for our own files: slskd and Navidrome own them. Peer paths
  (what a peer shares) are shown in the slskd and downloads tabs.
- **Ids**: requests, downloads and files are Post-Vinyl's numbers; catalog ids are the source's
  (`source` + id); library song ids are Navidrome's and change when a file moves.
- **Paging**: `page` starts at 1, `page_size` 1–100; answers carry `total`, `page`, `page_size`.

## Account

| Call | Who | Takes | Answers |
|---|---|---|---|
| `POST /api/auth/login` | anyone | `{username, password}` | `{username, is_admin}` + the cookie |
| `POST /api/auth/logout` | user | | `204` |
| `GET /api/auth/me` | user | | `{username, is_admin}` |
| `GET /api/system/ping` | anyone | | `{status: "ok"}` |
| `GET /api/me/credentials` | user | | `{listenbrainz: bool}`: which credentials are stored, never their values |
| `PUT /api/me/credentials` | user | `{listenbrainz_token}` (`null` removes it) | checked with ListenBrainz first: invalid `422`, ListenBrainz down `503` (the old one stays), ListenBrainz off `409` |

## Search tab (catalog + cart)

Searches go to the user's catalog sources in their order; the first that answers with results
is shown (`source`). Browsing stays on that source. A search shows as many results as the
user's `catalog.results_per_search` setting.

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/catalog/tracks` | user | `title`, `artist?` | `{source, items: [track], origin_id}` |
| `GET /api/catalog/albums` | user | `title`, `artist?` | `{source, items: [album], origin_id}` |
| `GET /api/catalog/artists` | user | `name` | `{source, items: [artist], origin_id}` |
| `GET /api/catalog/{source}/artists/{artist_id}/albums` | user | | `[album]` |
| `GET /api/catalog/{source}/albums/{album_id}/editions` | user | | `[edition]` |
| `GET /api/catalog/{source}/editions/{edition_id}/tracks` | user | | `[track]` with disc and position |
| `GET /api/catalog/{source}/covers/{cover_id}` | user | `size?` (32–600 px, default 120) | the cover a track, album, edition or artist names in its `cover_id` (artist pictures: Deezer only), from that source's images (Deezer's own; MusicBrainz's from the Cover Art Archive); cached a week by the browser; never stored; `404` when there is none, `404` while the source is off |
| `POST /api/requests` | user | `{source, track_id, origin_id?}` | `201` the request, already handed to the download source |
| `POST /api/requests/batch` | user | `{items: [{source, track_id, origin_id?}]}` (1–100: the browser's cart) | `207` one `{source, track_id, status, request?, error?}` per item |

## Requests

A request is one song someone asked for, whatever started it. `track_state`: `requested` →
`searching` → `found` / `notFound` → `midDownload` → `downloaded` / `notDownloaded` /
`badDownload`; `dropped` and `reRequested` from the user.

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/requests` | user | `page`, `page_size` | the user's requests, most recently used first |
| `GET /api/requests/{request_id}` | owner | | the request with `download_source`, `download_state` |
| `GET /api/requests/{request_id}/searches` | owner | | its slskd searches, newest first, every file labelled by the matcher (`correct`, `excluded:live`, `no_title`, `duration`, `locked`…) |
| `POST /api/requests/{request_id}/drop` | owner | | the request; a running download is cancelled (`409` once it has ended) |
| `POST /api/requests/{request_id}/re-request` | owner | | the request, handed to the download source again |

## slskd tab

| Call | Who | Takes | Answers |
|---|---|---|---|
| `POST /api/slskd/searches` | user | `{query}` | `202` the typed search's request. A repeated query re-reads its stored results; only one that ended `notFound` searches again |
| `GET /api/slskd/searches` | user | `limit?` | recent typed searches, each with its latest search's live state at slskd (no files) |
| `GET /api/slskd/searches/{request_id}` | owner | | its searches: `state`, `is_complete`, counts and, once complete, every file grouped by peer (`has_free_slot`, `upload_speed`, `queue_length`); excluded versions marked against the query. Poll until `is_complete` |
| `POST /api/slskd/downloads` | owner | `{request_id, files: [{username, filename}]}` | `201` one new request + song per file, queued as exactly that file. Each file must be in that search's results (`404` otherwise, nothing written); the queue paused → `409` |

## Downloads tab

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/downloads` | user | `state?` (`""` = all), `everyone?` (admins), `page`, `page_size?` | newest first: song, `track_state`, `state` (`queued`, `downloading`, `completed`, `failed`, `cancelled`), `attempts`, the running attempt (peer, peer path, rank), `library_id` (the song's Navidrome id once its file is indexed: its cover) and its live `progress` (`state`, `bytes_transferred`, `size`, `average_speed`, `place_in_queue`). Without `state` / `page_size` the user's own defaults apply |
| `GET /api/downloads/{download_id}` | owner, admin | | the same, every attempt with its `outcome`, and the peer is asked its queue place when the transfer waits in it |
| `POST /api/downloads/{download_id}/next-peer` | owner, admin | | skips the running peer (not counted against it) and queues the next one; `409` when nothing is running |
| `GET /api/admin/queue` | admin | | `{paused}` |
| `POST /api/admin/queue/pause` | admin | | no new searches, downloads or picks start; running downloads carry on; kept across restarts |
| `POST /api/admin/queue/resume` | admin | | waiting requests start within a minute |

## File management

The whole library as Navidrome indexes it. A rename is two steps: `rename` looks the new names
up (ListenBrainz with the admin's token, else a MusicBrainz search) and answers the suggested tag
set; then `apply` writes it, or, when nothing matched, `strip` takes every tag off and moves the
file by the new names. Files are never overwritten (`" (2)"`) and never touched outside the
library.

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/library/files` | user | `q?` (title, album, artists), `page`, `page_size` | songs with `has_cover`, and `file_id` / `file_state` where Post-Vinyl downloaded them |
| `POST /api/library/files/{song_id}/rename` | admin | `{title, artist, album_artist?, album?, year?, track?, disc?}` | `{match, recording_id, release_id, via, tags, has_cover}` or `{match: false}` |
| `POST /api/library/files/{song_id}/apply` | admin | `{recording_id, release_id}` | `{file}`: the song under its new id (`null` if Navidrome doesn't list it yet) |
| `POST /api/library/files/{song_id}/strip` | admin | `{title, artist}` | `{file}` at `<artist>/<title>` |
| `DELETE /api/library/files/{song_id}` | admin | | `204`; the file goes, Navidrome rescans; a later request downloads it again |
| `GET /api/library/files/{song_id}/cover` | user | `size?` (32–600 px, default 160) | the song's cover image from Navidrome (`has_cover` on the song says it has one), cached a day by the browser; never stored; `404` when there is none |

## Administering

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/admin/peers` | admin | | peers with a bad record: `failure_count`, `blocked_at` (set = banned) |
| `DELETE /api/admin/peers/{username}` | admin | | `{cleared: 0 \| 1}` |
| `DELETE /api/admin/peers` | admin | | `{cleared: n}` |
| `GET /api/admin/users` | admin | | `{id, username, is_admin, status, has_listenbrainz_token, created_at}` per user; no secrets |

## Settings

`GET /api/settings/schema` describes every field so the frontend can draw it: `key`, `section`,
`type` (`int`, `text`, `choice`, `ordered_list`, `sources`), `default`, `help`, `min` / `max` /
`step`, `choices`, `editable`, `applies` (`now` or `restart`). A `PUT` sends only the changed
keys; all are checked before any is stored (`422` names the first that doesn't fit).

| Call | Who | Takes | Answers |
|---|---|---|---|
| `GET /api/settings/schema` | user | | `{user: [field], install: [field]}` |
| `GET /api/me/settings` | user | | `{key: value}`: source order and off per kind (`{order, off}`), `catalog.results_per_search`, `lists.page_size` (every list), `downloads.state` |
| `PUT /api/me/settings` | user | `{changes: {key: value}}` | the new values; they apply at once |
| `GET /api/admin/settings` | admin | | `{values, restart_pending}`: the config file's values and which of them the running app doesn't use yet |
| `PUT /api/admin/settings` | admin | `{changes: {key: value}}` | the same. Written to the config file: only the changed lines, comments kept, a backup made next to it; applies at the next restart. Folders aren't editable (they match the Docker mounts) |
