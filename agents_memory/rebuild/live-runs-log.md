# Validation log

## 2026-10-19: live run on the isolated Docker test stack (branch core-rebuild, HEAD bb36301)

Stack: `docker compose -f docker-compose.test.yml --env-file .env.test -p postvinyl-test up -d --build`
Containers: postvinyl-test (app, built from working tree), navidrome-server-test, slskd-test,
postvinyl-proxy-test. Only the test stack was touched; the real `.env` and real stack were not.

### Health
- app `/api/system/ping`: 200; `/api/system/status`: slskd up (14 ms), navidrome up (4 ms)
- navidrome `/ping`: 200
- slskd `/health`: 200; `/api/v0/server`: Connected, LoggedIn
- proxy container: running (MusicBrainz and example.com fetched through it, both 200)

### Live requests
| Request | Result |
|---|---|
| `GET /api/musicbrainz/search/recordings?title=creep&artist=radiohead` | 200, real results (MBID fec5d8cf…, "Creep", score 100) |
| Deezer `api.deezer.com/search?q=radiohead creep` (from host) | 200, real results |
| `POST /api/search` "radiohead creep" | 201; slskd completed with 60 responses; `GET /api/searches/{id}` returns `completed` with results |

### Bugs found (not fixed in this run)
1. `GET /api/searches` (list) reports `searching` for a search that the detail endpoint shows as
   `completed`. The detail call is what completes it, so the list is stale until someone polls the
   detail. Affects any UI that only reads the list.
2. The download monitor errors on every poll: `DownloadStore` has no `fail_stale_pending` and no
   `get_adopted_live_transfers` (WIP store vs old worker). Expected phase 4 work; confirmed live.
3. `GET /api/musicbrainz/search/recordings` requires `title` (not `query`). Not a bug, but the
   parameter names differ from the search route; document in the API contract.
4. slskd dropped to "Failed to reconnect: timed out" once during the run and recovered on the
   next attempt (log 23:31). Connection is healthy now.

### Not yet verified
- Downloads end to end (blocked by bug 2).
- Navidrome indexing and `navidrome_id` linking.
- Tagging through beets (not exercised).

## 2026-10-19: live download on the isolated test stack (HEAD after phase 4)

Test library: `MUSIC_HOST_DIR` pointing at a scratch test library in `.env.test` only
(scratch dir, not the real library). Containers recreated with `--force-recreate`; mounts verified.

- `POST /api/search` "radiohead creep": completed, 60 results.
- `POST /api/queue` (peer danielbasta, one mp3): 201, `enqueued_count: 1`.
- Transfer reached 100% in about 4 minutes, average ~310 KB/s.
- DB (`app_data_test/musica.db`): `downloads` row 1 `completed`; `download_attempts` 1 row;
  `file_management` row `source=slskd, stage=moved` created by the handoff trigger.
- File on disk: `downloads/complete/soulseek/danielbasta/CD3/18 Radiohead - Creep (Radio Edit).mp3`.
- Monitor logs: no errors or tracebacks.

Findings:
1. The monitor warns "Exact path not found ... matched by basename". The peer path has a
   `CD3\` folder and backslashes; the path mapping misses it, and the basename fallback saves the
   move. Fix the path mapping in phase 4 or phase 5.
2. The file is marked `moved` but stays under the downloads tree. beets is disabled, so there is no
   library placement yet. Expected for now; phase 5 owns placement.
3. Not checked: whether slskd removed the finished transfer (history cleaner, design item D2).


## 2026-10-30: the app in its container on the test stack (#113, branch rebuild/container-test)

The test compose's `postvinyl` service now passes only the PV_* env (Navidrome and slskd by
service name, `PV_DEV=1`), mounts the config folder (`post-vinyl.yaml`) and the data folder;
the old app's env, `.env` and `config.toml` mounts are gone. Navidrome test data moved aside so
setup could run on an empty Navidrome.

- Found: the Dockerfile's `python -m app.main` had no entry point; now
  `uvicorn app.main:create_app_from_env --factory`.
- First admin: setup run by the user through the setup override on a real terminal; one admin
  row, `is_admin=1`. After `down` + `up -d`: container healthy, `/docs` 200, core loop started
  (0 waiting), no tracebacks.
- Login with the admin: 200, `/api/auth/me` 200.
- Config folder mounted `:ro`: `PUT /api/admin/settings` → 422. Found: the reason named our own
  file path (`/app/config/.post-vinyl.yaml.<n>.new`); now only `can't write post-vinyl.yaml:
  Read-only file system`. Writable mount: 200, `.bak` kept beside the file.
- Found: the healthcheck's `/api/system/ping` every 15 s filled the access log; now quiet
  (0 lines after the rebuild).
- 60 s Navidrome wait: Navidrome paused 25 s (a stopped Navidrome can't be used: the setup
  container shares its network namespace), setup waited, then refused with "already has an
  admin"; still one user.
- The key-generation command from the README runs in the app image.

Not verified: the server's own compose with the new `postvinyl` service and
`ND_MUSICFOLDER=/music/library` (cutover); PowerShell.

## 2026-10-30: folder names on the test stack (#118, branch rebuild/folder-names)

- `docker compose config` resolves for the base compose, base + setup override (with
  `.env.example`), the test compose and test + setup override (setup mounts
  `./postvinyl_data_test`).
- Test stack rebuilt on `./postvinyl_data_test` and `./postvinyl_config_test` (the old test
  folders moved, database kept): healthy; inside, `PV_DB_PATH` and `PV_CONFIG_PATH` come from
  the image defaults, `/app/data` and `/app/config` no longer exist; login 200; a settings write
  landed in `postvinyl_config_test/post-vinyl.yaml` with its `.bak` (reverted after).
- The image on an empty data folder with only env + mounts: starts, creates `post-vinyl.db`.
- Found (for #115): with slskd unreachable the cleanup worker logs a full traceback every tick;
  the app keeps running.

Not run: first-admin setup on the new folders (the setup override's mount resolves to them;
Navidrome already has its admin).

## 2026-10-30: catalog, files, tagging and upkeep workers in the container (#114)

Test stack, app in its container. Worker intervals lowered in the test config (retag 5 min,
missing check 5 min, unverifiable kept 1 h, redispatch and cleanup 10 s). Songs picked from
catalog data; real peer files from the test slskd.

Verified:
- Concurrency: 8 parallel `POST /api/requests` for one catalog track → 8 requests, 1 song;
  6 parallel drops of one request → one 200, five 409. No lock errors in the log.
- Album and artist results keep MusicBrainz's order (Homogenic, Blue Lines, Portishead, Air:
  25 artists, same ids in the same order as a direct MusicBrainz query).
- Real M4A (ALAC), Ogg Vorbis and Opus peer files: renamed via MusicBrainz, full tag set
  written, moved to `<album artist>/<album (year)>/<nn> <title>`; M4A got its cover (Ogg/Opus
  don't, by design); the M4A's ReplayGain tags survived.
- A file the pipeline still holds isn't in Navidrome: rename and strip answer 404, nothing moves.
- Retag: an slskd-tab pick without a ListenBrainz token failed tagging, was retried 5 min
  later, failed again, moved into the library untagged and was indexed on the same tick.
- Re-dispatch: a request made with the download source off waited; turned back on, it was
  searching 6 s later.
- Missing: a library file deleted outside Post-Vinyl → `missing` at the next check; a file
  deleted from downloads while it waited for its retag → `missing`.
- Replacement at another path: a re-request's FLAC landed at `Radiohead/Creep.flac`; the old
  `Pablo Honey (1993)/02 Creep.ogg` was deleted.
- Cleanup: none of the run's transfers stayed listed at slskd; failed attempts' folders deleted.
- Unverifiable: a download overwritten with non-audio bytes → `not_audio`, kept 1 h, then
  deleted (row `missing`, code kept).

Found and fixed:
- The missing check ran during a rename's rescan and marked the just-renamed file missing (one
  lock now covers moves, renames, deletes and the missing check; regression test).
- Opus loudness (`R128_*`) wasn't kept on retag; now kept with ReplayGain and lyrics.
- A cut-off MP3 (4 KB, its Xing header claiming 306 s) passed verification, was tagged and
  went into the library. Verification now fails a file smaller than 0.8 × bitrate × length
  (`truncated`; not FLAC, whose bitrate is derived from its size). On all 26 audio files of the
  test stack only that MP3 is flagged. The cut-off MP3 stays in the test library.

Seen on the way (for #115): `Completed, Aborted` (remote size changed), `tooSlow` three times,
`Completed, Rejected: File not shared`. Matcher: "drumless edition", "(Mazaruni Dub One)" and a
"BBC Radio One Session" weren't marked as excluded versions. One rename of Creep found no match
and matched on the retry a minute later; no error was logged, not reproduced.

Not verified (reasons in unverified-live.md): ListenBrainz as a catalog source (no caller
before milestones 9–10), an old copy outside the library, the album-only and earliest-official
fallbacks, ListenBrainz rejecting a mismatch (tried twice, no case came up), lyrics surviving,
Navidrome not listing a file yet, the server's Navidrome. MusicBrainz paging past 100 isn't
built (Creep has 194 hits).

## 2026-10-30: slskd gate, search and download failures in the container (#115)

Test stack, app in its container; slskd logged out, stopped and restarted on purpose.

Verified:
- The ladder: an obscure catalog song went L0 → TA → T with 2 helpful peers and was found.
- `notFound`: another went L0 → TA → T with 0 helpful peers; slskd shows the "2020" check
  (600 responses, healthy) before the request ended `notFound` (a passing check isn't logged).
- Gate: slskd logged out (`DELETE /server`) → no search started for 2.5 min, one warning a
  minute; reconnected → searched within 40 s. After restarts slskd was logged in but without
  a distributed parent for ~5 min; the gate waited the whole time.
- slskd stopped mid-search: "lost readiness during search; running it again", rerun once
  slskd was back.
- Ban: a peer's third failure banned it (`blocked_at` set).
- Dropping a request mid-transfer: transfer cancelled and gone at slskd, attempt folder
  removed, peer not blamed.
- Seen in #114: `Completed, Aborted` (remote size changed), `tooSlow`, `File not shared`;
  `badDownload` after a broken last attempt.
- After the fixes, slskd restarted mid-transfer: one "unavailable" and one "back" line, the
  attempt `interrupted`, the same file from the same peer queued again into its own folder and
  completed, the peer not blamed, no tracebacks.

Found and fixed:
- slskd's own outage blamed peers: the cut-off transfer ("Application shut down") and the next
  queue (HTTP 500 "must be connected and logged in") each counted a failure, and 2 of 5
  candidates went. Now: nothing is queued while slskd isn't logged in; such attempts end
  `interrupted` (not blamed, no candidate used up, the same file again). `'interrupted'` (and
  #111's `'skipped'`) folded into `001_initial.sql` (user 2026-10-30); the test database was
  recreated.
- That change opened a race (a poll between a download's row and its first attempt queued
  the file twice, slskd refused the copy "Already in progress", the peer got banned): the
  first queueing now runs under the downloader's lock (regression test).
- slskd 0.26.2 re-requested a cut-off file by itself on startup, saving it to
  `<peer>/<remote folder>/` instead of our attempt folder: that copy is now forgotten before
  ours is queued (unit-tested; the next live restart had no such resume).
- slskd unreachable: the cleanup worker logged a traceback every tick, and the deletion of
  unverifiable downloads ran in the same tick, so it never ran while slskd was down. Now its
  own worker; slskd errors are one line per outage (cleanup and download monitor).

Not verified (reasons in unverified-live.md): a parent lost while slskd stays up, a throttled
account, a crash while slskd is up, slskd refusing a query, a cancelled search that never
ends, a ban expiring, a peer going offline mid-transfer, slskd's own resume live, a peer
erroring on the queue-place ask.
