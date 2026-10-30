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
