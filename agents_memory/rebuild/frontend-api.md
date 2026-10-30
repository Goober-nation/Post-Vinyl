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
- Settings (user, 2026-10-30, #109):
  - **Admin settings** (the YAML's sources, slskd and workers sections) are edited through the
    API **without dropping the YAML's comments**: only the changed `key: value` lines are
    rewritten (a value written over several lines is refused: edit that by hand). The file is
    backed up first; the new values apply **at the next restart** (the API says so).
  - **Folders** (`files`) are shown, not editable: they must match the Docker mounts.
  - **Per-user settings**: source order and off per kind; how many results a catalog search
    shows; the downloads tab's default state filter and page size.
  - **Schema**: per field its key, section, type (int, text, choice, ordered list), min/max/step,
    choices, default, help and whether it applies now or at restart, generated from the settings
    dataclasses so it can't drift from the code.
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
- Stack and design kit: decided, see **Frontend stack** below (user, 2026-10-30).

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
| settings | ✅ `GET /api/settings/schema`; `GET`/`PUT /api/me/settings`; `GET`/`PUT /api/admin/settings` | #109; install edits keep the YAML's comments and apply at restart. The full contract: `docs/api.md` |
| recs (m9) | `POST /api/recs/radio {seed}`; `GET`/`PUT /api/recs/auto` | pull schedule, rotation |
| playlists (m10–11) | `GET /api/playlists`; `POST`/`PUT /api/playlists/{id}`; `POST /api/playlists/import-nsp`; `GET /api/playlists/{id}/nsp` | |
| imports (m9–11) | `POST /api/imports {source, kind, link\|file}`; `GET /api/imports`; `POST /api/imports/{id}/pull`; `GET /api/imports/formats` | per source and per batch |

## Frontend stack (user, 2026-10-30)

- **TypeScript, no UI framework** (plain DOM; a small render helper if lists get repetitive),
  **Tailwind CSS v4 + daisyUI 5** for styling and components, built with **Vite**. Chosen over
  plain JS because a build step is needed for Tailwind anyway, and types catch API drift.
  daisyUI is CSS-only and themed through CSS variables, so it works without a framework and
  gives the parts the tabs need (tables, tabs, range sliders, modals, badges, progress, toasts).
- **API types are generated, not hand-written**: `openapi-typescript` turns FastAPI's
  `/openapi.json` (dev only) into types, committed with the frontend; regenerate after any API
  change, so a renamed or dropped field is a compile error. The state strings (`track_state`,
  `download_state`, `file_state`, matcher labels) come out as unions.
- **Checks**: `tsc --noEmit` runs with the build (the frontend's mypy).
- **Build-time only, one container**: a Node stage in the existing multi-stage `Dockerfile`
  (`node:22-slim`, `npm ci`, `npm run build`) copies the static output into the Python runtime
  image; Node and `node_modules` never ship. It runs as part of `docker compose up -d --build`
  (the stage is rebuilt only when `web/` changes; `npm ci` is cached until the package files
  change). FastAPI serves the files (`StaticFiles`) on the same origin as the API, so the
  session cookie and the Origin/CSRF check (#88) work unchanged, no CORS.
- **Development**: `npm run dev` (Vite dev server, live reload) on the host, proxying `/api` to
  the app; no extra container.
- **Built in #119** (2026-10-30): `web/` (Vite builds to `web/dist`, which `create_app` mounts
  at `/` when it exists). Default deny lets `GET /` and `/assets/*` through without a session
  (no data in them); everything else stays denied. Beets was already gone from the `Dockerfile`.
  **TypeScript stays on 5.x**: openapi-typescript 7.13 declares a `typescript ^5` peer, which
  `npm ci` in `node:22-slim` enforces, and TypeScript 7 lacks the JS compiler API it uses (live
  2026-10-30); lift the cap when openapi-typescript supports a newer one.
- **Hash routes** (`#/slskd`, `#/downloads`…; user, 2026-10-30): a reload keeps the tab, the
  session cookie keeps the login.
- **Admin-only parts are shown to everyone** and refuse with "Admins only" for non-admins (user,
  2026-10-30, replacing "hide them"); the API's 403 stays the real guard.
- Commands, from `web/`: `npm ci`; `npm run build` (`tsc --noEmit` + Vite);
  `PV_API=<the app's PV_PUBLIC_ORIGIN> npm run dev` (proxies `/api`, sending that origin so the
  CSRF check passes); `PV_API=… npm run gen:api` against an app run with `PV_DEV=1` after any
  API change.
- **Design kit built in #120** (user approved the direction, 2026-10-30): SoundCloud's layout
  (top bar with tabs and search, a main list with a right rail, a fixed bottom bar showing
  download activity where SoundCloud has its player; the waveform progress was tried and dropped, user 2026-10-30: progress is "62% · 1.2 MB/s" as text), in calm
  deep blue: navy page, one blue accent, hairline borders, flat surfaces, 3–6 px corners; no
  gradients, glass, glow or soft oversized cards. Fonts IBM Plex Sans + Plex Mono, bundled
  (@fontsource), so no request leaves the origin. Themes `harbour-dark` (default) and
  `harbour-light` in `web/src/style.css`; components in `web/src/ui.ts`; every one on `#/kit`
  in `npm run dev`; the static mockup is `web/design/mockup.html`. A state is always a dot
  plus words, never colour alone. The ui-ux-pro-max skill was used for its rules (contrast,
  focus, density, reduced motion); its suggested indigo/green palette was rejected as generic.
- **Identity and palette (user, 2026-10-30)**, replacing the navy above: blue-tinted charcoal
  (`#0C0E12` page, `#13161C` panels) with a desaturated blue accent (`#7EA3CF`), because the
  navy was too blue ("eye burns"); every text pair checked at ≥ 5.6:1. Identity, all from the
  record world:
  - **Logo**: a square record with slightly rounded corners, grooves and a warm amber label
    (`web/src/logo.svg`, also the favicon). **Wordmark and page titles in Forta**, the legacy
    app's logo font (OFL, copied with its licence to `web/src/fonts/`); body stays IBM Plex.
  - **Record motif**: what is happening now shows a small turning record (still with reduced
    motion). **Warm amber `--live`** is used only for live things (downloading, progress, the
    logo's label); blue stays for actions, links, focus and the active tab. Queued = hollow dot.
  - **Sleeve typography**: side/track before titles (`A3`), artists as small-caps credits,
    catalogue numbers (`PV-0142`), small-caps rail headings.
  - **Album art colour**: the chrome stays neutral; a song's cover colour (`--art`) tints its
    cover, the selected row's edge and the bottom bar's top edge (real cover colours from
    Navidrome's art when the tabs are built).
- Earlier plan: tokens first (colors with light + dark, a UI font and a monospace font for paths and
  peer names, type/spacing/radius scales, compact density for the tables), then the ~15 shared
  components, then mockups of the hardest screens (slskd tab, downloads queue, settings).
  Soulbeet (Dioxus fullstack, Rust → WebAssembly, Tailwind v4, WebSocket push) was looked at;
  only the Tailwind choice carries over.
