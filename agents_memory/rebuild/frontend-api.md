# Frontend tabs and their API

The pages the frontend (milestone 8) will have, from the user (2026-10-30), and the endpoints
behind them. **Every endpoint for the core tabs is built in milestone 7** (user, 2026-10-30), so
milestone 8 only builds pages. Recommendations, playlists and imports need the services of
milestones 9–11 and are built there. The list guides the API; it doesn't limit it.

Decisions (user, 2026-10-30):
- **Download cart lives in the browser**; one batch call sends it.
- **Live updates by polling** (1–2 s while a tab is open); successful (200) polling calls are
  **not written to the access log**.
- **Each checked slskd file becomes its own request and song**, built from the file's data
  (not from a catalog), tagged later from MusicBrainz like any other download.
- Paths stay slskd's and Navidrome's: no endpoint returns a file path (#107).
- **Rename = fix the tags, then re-place** the file where the tags put it; Navidrome rescans.
  **Delete is admin-only** (it removes the file for every user).
- File management (user, 2026-10-30, #112):
  - The list is the **whole library**, read from Navidrome; files Post-Vinyl downloaded show its
    history too. Rename and delete work on any of them.
  - **Rename** edits the core set (title, artist, album artist, album, year, track, disc).
    New names can mean a different MusicBrainz match, so Post-Vinyl **looks them up again** and
    **suggests the full new tag set** when a release matches; the user accepts it. When nothing
    matches, the user may **strip the tags** and the file moves by the new names, untagged.
    The other tags (MusicBrainz ids, ISRC, cover, ReplayGain, lyrics) stay unless replaced.
    **Rename is admin-only** (it changes the file for everyone). The lookup tries
    **ListenBrainz first** with the renaming admin's token (same checks as tagging: main artist
    in the credit, length within ±6 s) and **falls back to a MusicBrainz search** when there is
    no token or ListenBrainz finds nothing, mismatches or fails.
  - **Delete**: the file goes from disk (inside the library only), Post-Vinyl's record is
    marked deleted, Navidrome rescans. A later request downloads the song again.
  - **ListenBrainz token**: checked with ListenBrainz before it's stored; an invalid one is
    refused (422); ListenBrainz down → 503, the old token kept.
- Issues: #108 slskd tab, #111 downloads tab, #112 files + admin, #109 settings + `docs/api.md`.

## Tabs (user's list)

- **slskd**: search, check what to download, send to the queue.
- **Search**: catalog; download cart.
- **Downloads**: queue with status and sources; administering the downloads and the queue.
- **Recommendations** (milestone 9): radio; autonomous recommendations (auto recs); upload
  preferences from data passing; base recs playlists (radios for playlists, recurring pulls for
  auto recs, the rotation setting).
- **Playlists** (milestones 10–11): every playlist Post-Vinyl defines, recs-based included;
  NSP-type playlists; the full NSP setting suite; import likes from data passing.
- **File management**: rename, delete.
- **Administering**: data passing from ListenBrainz, Navidrome, Yandex… per user (Last.fm the
  next candidate), with instructions for converting other platforms' exports to CSV/JSON, data
  kept apart per source and per batch/playlist, and re-pulls of the exact same source (same
  playlist link, same LB user); user credentials; banned peers and clearing them.
- **Settings**, per user, with real fields (sliders and the like where they make sense).
- Design kit: open, decided in milestone 8. Soulbeet uses Dioxus (Rust → WebAssembly) with
  Tailwind CSS v4, which doesn't carry over to this stack.

## Endpoints

✅ = exists. Everything else is milestone 7 unless a milestone is named.

| Area | Endpoint | Notes |
|---|---|---|
| auth | ✅ `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | #88 |
| catalog | ✅ `GET /api/catalog/tracks\|albums\|artists`; browse `{source}/artists/{id}/albums`, `albums/{id}/editions`, `editions/{id}/tracks` | #107 |
| requests | ✅ `POST /api/requests`, `GET /api/requests`, `GET /api/requests/{id}`, `POST …/drop`, `POST …/re-request` | #107 |
| cart | ✅ `POST /api/requests/batch {items: [{source, track_id}]}` | 207, a result per item (#111) |
| slskd | ✅ `POST /api/slskd/searches {query}`; `GET /api/slskd/searches`; `GET /api/slskd/searches/{request_id}` | state read live from slskd by id; files grouped by peer, excluded versions marked |
| slskd | ✅ `GET /api/requests/{id}/searches` | a request's own searches, full matcher marks |
| slskd | ✅ `POST /api/slskd/downloads {request_id, files: [{username, filename}]}` | one request + song per file |
| downloads | ✅ `GET /api/downloads?state=&page=&everyone=`; `GET /api/downloads/{id}` | live progress from slskd; the detail asks the queue place (#111) |
| downloads | ✅ `POST /api/downloads/{id}/next-peer`; `GET /api/admin/queue`, `POST /api/admin/queue/pause\|resume` | #111 |
| files | `GET /api/library/files?q=&page=`; `POST /api/library/files/{id}/rename` (suggestion) → `…/apply` or `…/strip`; `DELETE /api/library/files/{id}` | ✅ #112; `{id}` is Navidrome's, new after a move (it follows the path); rename/delete admin-only |
| admin | ✅ `GET /api/admin/peers`; `DELETE /api/admin/peers/{username}`; `DELETE /api/admin/peers` | peers with a bad record (#112) |
| admin | ✅ `GET /api/admin/users` | Navidrome owns the accounts; no secrets (#112) |
| me | ✅ `GET`/`PUT /api/me/credentials` | write-only; ListenBrainz token checked first (#112) |
| settings | `GET /api/settings/schema`; `GET`/`PUT /api/me/settings`; `GET`/`PUT /api/admin/settings` | the schema gives type, min/max/step, choices, help; the frontend draws fields from it |
| recs (m9) | `POST /api/recs/radio {seed}`; `GET`/`PUT /api/recs/auto` | pull schedule, rotation |
| playlists (m10–11) | `GET /api/playlists`; `POST`/`PUT /api/playlists/{id}`; `POST /api/playlists/import-nsp`; `GET /api/playlists/{id}/nsp` | |
| imports (m9–11) | `POST /api/imports {source, kind, link\|file}`; `GET /api/imports`; `POST /api/imports/{id}/pull`; `GET /api/imports/formats` | per source and per batch |
