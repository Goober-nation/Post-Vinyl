# Rebuild work plan & rules (carried over from backlog2, dropped 2026-10-26)

Goal: one clean core loop: **request → slskd search → download → file lifecycle → Navidrome
link**. Design lives in this folder (`core-db-redesign.md`, `users-auth.md`,
`design-notes-compilation.md`, `search-test-v2.md`); this file holds the work order, the rules and
the leftovers. Tracking: **GitHub issues + milestones** (user, 2026-10-26). Milestones 1–10 created 2026-10-27 on
Goober-nation/Post-Vinyl (public, self-contained descriptions): 1 normalization research, 2 search
test v2, 3 service clients, 4 data layer + login, 5 core logic, 6 core loop, 7 wiring +
validation, 8 approval inbox + Postradio, 9 Yandex playlists, 10 custom playlists. Issues are
created when a milestone starts.

## Rules (kept from backlog2)
- **Ponytail ladder** at every design and code decision; stop at the first rung that holds:
  1. Does this need to exist? 2. Already in this codebase? 3. Standard library? 4. Native platform
  feature? 5. Installed dependency? 6. One line? 7. Only then the minimum that works.
  Never trim trust-boundary validation, data-loss handling, security or accessibility.
- **Long tests run detached**; check them periodically and replace a test that runs too long with
  a shorter one rather than waiting.
- **Done** = tests pass, ruff and mypy clean for touched files, and a real-service run recorded
  for any integration change. Unverified items are marked as such, never ticked.
- **Tests run in Docker** (a recommendation, now in AGENTS.md; tests only where they pay off): unit/DB tests in a throwaway `python:3.12-slim` container with the repo
  mounted read-only; live tests on the isolated test stack (`docker-compose.test.yml`,
  `.env.test`, separate test Navidrome + slskd users). Never run compose or setup flows against the
  real `.env`.
- **Branches (2026-10-27, replaces "work on dev")**: rebuild work on `rebuild/*` branches off and
  merged into `rebuild/base`; nothing reaches `main` until the rebuild is done. Old app in
  `legacy/` for reference.
- **Config**: thresholds are config values when they're **user-facing or advanced** settings
  (internal constants may stay in code); variable names must be self-explanatory. **The config
  layers themselves are up for redesign** (not yet planned).

## Decided 2026-10-26 (from the backlog interview)
- **Stall rule** (attempt outcomes): **`noStart`** = the transfer hasn't started 20 s after slskd
  first reports it as downloading (time spent queued doesn't count); **`tooSlow`** = average speed
  over the last 20 s below **40 kbit/s** (≈ 5 KB/s).
- **Recent searches (#13)**: no delete/hide for now; the list is **paginated** (newest page through
  an index), so UI loading time doesn't grow with history. Requests are never pruned.

## Work order (from backlog2 phases; superseded by the build order in `architecture.md` §6)
0. **Setup**: Docker test runner; baseline for the full suite + ruff + mypy; confirm the test
   stack never touches `.env` (see `../topics/naked-clone-testing.md`).
1. **DB**: migration 015 reviewed against `core-db-redesign.md`; migration 016 users/sessions +
   auth (`users-auth.md`); songs, song_identity, track/file states, richer attempts, tagging,
   `strategy` column; stores with no SQL outside them; tests for the trigger, the retry count and
   FK cascades. (Done so far: FK pragma on, SearchStore removed, `tests/test_db` green.)
2. **Interfaces**: ABCs for the core loop (search, download, library/file lifecycle, requests),
   extending `app/services/interfaces/`, with fakes for tests. `RequestService.create` takes
   `user_id` + `song_id` now, not `mbid`. Not designed yet.
3. **Search**: normalization test → search test v2 → final query order; stable-count exit
   (8 s / 2 s / 5 s min / 30 s / cap 600); file matcher (`design-notes-compilation.md`).
4. **Downloads**: row written before calling slskd; retry on the same row; attempts with
   rank/outcome; stall rule above; peers; ignore transfers the app didn't start.
5. **Files**: handoff, `file_state` flow, own MusicBrainz/ListenBrainz tagging (no beets),
   Navidrome link by `navidrome_id` (no placed path saved), HistoryCleaner becomes the
   "delete handed-off downloads at slskd + locally" pass.
6. **Wiring**: routes and workers call only the new core; config UI; API contract + `docs/api.md`.
7. **Validation**: end-to-end on the test stack, failure paths, report separating verified from
   unverified; log runs in `live-runs-log.md`.

## Known bugs (from the 2026-10-19 live runs; fix in the relevant step)
- `GET /api/searches` (list) shows `searching` for searches the detail endpoint shows `completed`
  (step 3/6).
- Download path mapping misses peer paths with a subfolder like `CD3\` and backslashes; the
  basename fallback hides it (step 4/5).
- Unverified whether slskd removes finished transfers (history cleaner) (step 5).
- `GET /api/musicbrainz/search/recordings` takes `title`, not `query` like the search route:
  document or align in the API contract (step 6).
