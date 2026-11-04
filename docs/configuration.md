# Configuration

Three levels, narrowest last:

| Level | Where | Who | Applies |
|---|---|---|---|
| Environment | compose `environment:` or `.env` | operator | at start |
| Install settings | `post-vinyl.yaml`, or **Settings > Server-wide** | admin | at restart |
| User settings | **Settings > Personal** | each user | at once |

A missing or invalid required value stops the app at start with a clear error, as does an unknown
setting or source name in the YAML.

## Environment

| Value | Required | Meaning |
|---|---|---|
| `PV_NAVIDROME_URL` | yes | Navidrome's address |
| `PV_SLSKD_URL` | yes | slskd's address |
| `PV_SLSKD_API_KEY` | yes | An slskd API key |
| `PV_DB_PATH` | image default | `/postvinyl/data/post-vinyl.db` |
| `PV_CONFIG_PATH` | image default | `/postvinyl/config/post-vinyl.yaml` |
| `PV_SECRET_KEY` | no | Fernet key or keys. Unset, one is generated at `secret.key` beside the database |
| `PV_PUBLIC_ORIGIN` | no | `scheme://host[:port]`. Unset, the CSRF check compares with `Host` |
| `PV_DEV` | no | `1`, `true` or `yes` serves `/docs` and `/openapi.json` |
| `PUID`, `PGID` | no | The user and group the app runs as (default 1000) |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` | no | Outgoing proxy, for hosts that can't reach ListenBrainz or MusicBrainz |

Secrets stay in the environment, never in the YAML.

## Install settings

`post-vinyl.yaml` is created from `post-vinyl.example.yaml`, with each value explained. Every value
except `sources` has a default.

Editing from the app rewrites only the changed lines and keeps comments. It checks the result loads,
keeps a `post-vinyl.yaml.<time>.bak` beside the file, and lists what's waiting for a restart. A value
written over several lines has to be edited by hand. If the config folder is read-only the save is
refused.

### `sources`

An ordered list per kind: order is priority, and a source left out is off.

| Key | Possible values | Default in the example |
|---|---|---|
| `sources.catalog` | `musicbrainz`, `deezer`, `listenbrainz` | all three |
| `sources.download` | `slskd` | `slskd` |
| `sources.request` | `postradio`, `yandex` (not built, accepted and ignored) | empty |

`listenbrainz` is never searched: it supplies pulls, name lookups, top tracks and similar artists.

Turning a source off never deletes anything. Work that needs it is refused with `409` and nothing
is sent. A request whose only download source is off waits.

### `slskd`

| Key | Default | Range | Meaning |
|---|---|---|---|
| `searches_per_minute` | 4 | 1–10 | Searches started per minute at most. About 5–6 got an account throttled |
| `throttle_check_query` | `"2020"` | | Search that proves the account isn't throttled. Keep it quoted: it must be text |
| `throttle_check_min_responses` | 300 | 50–600 | Responses that count as healthy |
| `throttle_pause_minutes` | 10 | 1–60 | How long a failed check pauses all searching |
| `queue_wait_seconds` | 15 | 5–120 | A peer keeping a download queued longer is skipped and counted as a failure. Hand-picked files wait without limit |
| `candidates_per_download` | 5 | 1–20 | Peers tried for one download |
| `peer_ban_failures` | 3 | 1–20 | Failures before a ban |
| `peer_ban_days` | 7 | 1–90 | Length of a ban |

### `files`

Shown in the app, editable only in the file.

| Key | Default | Meaning |
|---|---|---|
| `music_dir` | `/music` | The music mount inside the container |
| `slskd_downloads_dir` | `downloads/complete/soulseek` | Inside `music_dir`: slskd's downloads folder |
| `library_dir` | `library` | Inside `music_dir`: must be Navidrome's music folder |

### `workers`

| Key | Default | Range | Meaning |
|---|---|---|---|
| `pipeline_every_seconds` | 10 | 5–300 | Verify, tag, move, scan |
| `retag_after_seconds` | 30 | 10–3600 | Wait before the second tagging try |
| `missing_check_minutes` | 60 | 5–1440 | How often vanished files are marked missing |
| `redispatch_every_seconds` | 60 | 10–600 | How often waiting requests are started |
| `cleanup_every_seconds` | 60 | 10–600 | Clear finished transfers and leftovers |
| `unverifiable_keep_hours` | 24 | 1–720 | Keep downloads that failed verification this long |

## User settings

| Key | Default | Range | Meaning |
|---|---|---|---|
| `sources.catalog`, `sources.download` | install order | | `{order, off}`: reorder or switch off listed sources. A user can't add a source the install doesn't list |
| `catalog.results_per_search` | 25 | 5–100 | Results a catalog search shows |
| `lists.page_size` | 20 | 10–100 | Rows per page in every list |
| `downloads.state` | all | | The Downloads tab's first filter |

A source newly added to the install is on for everyone and goes after their chosen order.

The settings screens are drawn from `GET /api/settings/schema`, generated from the dataclasses in
`app/config.py`, so they can't drift from the code.

## Not settings

Constants in the code, because they came from measurements or rules, not preferences:

| Value | Meaning | Where |
|---|---|---|
| 8 s stable, 2 s poll, 5 s minimum, 30 s timeout, 600 responses | Search stop rule | `app/services/slskd_search.py` |
| 5 helpful peers | Ends the query ladder | `app/services/slskd_search.py` |
| 20 s, 40 kbit/s | Stall rule | `app/core/stall.py` |
| ±6 s | Length tolerance | `app/core/matcher.py` |
| 2 s, 5 s | Download monitor, pull workers | `app/workers/` |
| 20 failed logins an hour | Login cap | `app/services/auth.py` |
