# Post-Vinyl v0.1 🎉

## TL;DR

Post-Vinyl is an opinionated self-hosted music library that tries to simplify hosting your own music. Search for a song/album/artist (or
let it pull ListenBrainz recommendations on its own), and it takes care of the rest:

**MusicBrainz search → Soulseek download → beets tag/file → Navidrome library**, hands-off.

1. You search MusicBrainz (or a recommendation category fires automatically) for a recording, album,
   or artist.
2. Post-Vinyl resolves that to the *actual* canonical release and searches Soulseek for a matching
   file — not just whatever a random peer's file happens to be labeled.
3. The download is pulled in over the Soulseek network via [slskd](https://github.com/slskd/slskd).
4. [beets](https://beets.io/) tags, renames, and files the completed download into your library
   automatically, pinned to the release Post-Vinyl actually resolved.
5. [Navidrome](https://www.navidrome.org/) indexes it — the track shows up in your library and, for
   recommendations, in its category playlist — ready to stream from any Subsonic-compatible app. For 
   computer usage I just use the Navidrome web app, but [Feishin](https://github.com/jeffvli/feishin/releases)
   has many cool features, like an equalizer and granular settings, as well as being (to my knowledge) the only 
   Navidrome app that can create smart .nsp playlists inside its UI. 
   For mobile use i have tested literally everything there is on the IOS app store, and found 
   [Arpeggi](https://github.com/argie-w/Arpeggi-App) to be the absolute best looking and feeling app there is. 
   Honorable mention to [Yuzic](https://github.com/eftpmc/yuzic) for being (as far as I know)
   the only player to atempt to integrate slskd downloading directly within the mobile app, though i did not get is to work for me.

**You interact with it** through a small web UI (manual search, download queue, recs status, config)
and its REST API — see [`docs/api.md`](docs/api.md). Everything also runs unattended: recommendations
pull on a schedule, favorites sync both ways with ListenBrainz, and completed imports need no manual
step.

**Docker is a prerequisite** — Post-Vinyl ships as a Docker Compose stack (Navidrome + slskd + the app
itself) and isn't designed to be run any other way. See [`docs/deployment.md`](docs/deployment.md) to
get it running.

**On Soulseek sharing**: slskd shares your library back to the Soulseek network by default, same as
the official client. That's a good thing, not a side effect to work around — Soulseek only works
because everyone using it shares back; being a good peer is what keeps the network alive for
everyone, including you.

---

Post-Vinyl automates a self-hosted music library: it pulls recommendations from ListenBrainz, finds and
downloads matching tracks over Soulseek (via [slskd](https://github.com/slskd/slskd)), tags and files
them with [beets](https://beets.io/), and serves the result through
[Navidrome](https://www.navidrome.org/). You can also search and download manually, or browse
MusicBrainz and queue a specific recording or album directly.

It is one Docker Compose stack that wires those four systems together and adds the glue none of them
provide on their own: a search-and-download pipeline tuned for Soulseek's quirks, three
recommendation categories with their own pull/rotation logic, and a small web UI to watch and control
all of it.

For how it works internally, see [`docs/architecture.md`](docs/architecture.md). For every HTTP
endpoint, see [`docs/api.md`](docs/api.md). For running your own stack, see
[`docs/deployment.md`](docs/deployment.md).

---

## What it does

- **Recommendations** — three ListenBrainz-backed categories, each with its own pool, pull cadence,
  and destination playlist:
  - **Comfort Zone** — your top/similar-artist recommendations (1000-track rotating pool).
  - **Fresh Picks** — new releases, not personalized (a global feed, by design).
  - **Deep Cuts** — ListenBrainz's own weekly-generated "Daily Jams"/periodic-jams playlists.
  
  Each category can be pulled on a timer or manually from the UI. See
  [`docs/architecture.md#recommendations`](docs/architecture.md#recommendations) for the pull/dedup
  mechanics of each.
- **Manual search & download** — search Soulseek directly and queue any result.
- **MusicBrainz browse & download** — search MusicBrainz for a recording, album, or artist and queue a
  download resolved to that canonical release, not just whatever a peer's file happens to be tagged as.
- **Automatic tagging & filing** — every completed download is handed to beets, which tags, renames,
  and moves it into your library (or the matching recommendation category folder).
- **Navidrome sync** — starred (loved) tracks push a ListenBrainz "love"; tracks moved to Navidrome's
  Trash playlist get deleted from disk and sent a ListenBrainz "hate".
- **Live status** — an SSE event stream and small web UI show searches, transfers, and recs pulls as
  they happen.

## Quick start

The same commands work in bash, PowerShell and the macOS terminal. Needs Docker Compose 2.24 or newer.

1. Create Post-Vinyl's two folders and its settings file (each value is explained in it):
   ```bash
   mkdir postvinyl_config postvinyl_data
   cp config/post-vinyl.example.yaml postvinyl_config/post-vinyl.yaml
   ```
   On Linux, make both folders writable by UID 1000 (the container's user).
2. Copy `.env.example` to `.env` and fill it in (table below). Generate `PV_SECRET_KEY` once and
   back it up outside the database (losing it loses every stored secret):
   ```bash
   docker compose run --rm --no-deps postvinyl python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. Create the first admin (Navidrome's and Post-Vinyl's), once. Navidrome stays unpublished while it runs:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.setup.yml run --rm setup
   docker compose down
   ```
4. Start the stack and open `PV_PUBLIC_ORIGIN`:
   ```bash
   docker compose up -d
   ```

### Environment variables

Each can go in `.env` (read through `${...}` in `docker-compose.yml`) or straight into the service's
`environment:` in `docker-compose.yml`. Keep secrets in `.env`, since compose files get copied and shared.

| Variable | Needed | What it is |
|---|---|---|
| `MUSIC_HOST_DIR` | yes | Your music folder; must exist. Navidrome's library is its `library/` subfolder. |
| `SLSKD_DOWNLOADS_DIR` | yes | Where slskd saves finished downloads inside `/music` (default in `.env.example`). |
| `PV_SECRET_KEY` | yes, secret | Encrypts stored per-user secrets. |
| `PV_PUBLIC_ORIGIN` | yes | The address people open the app at, `scheme://host[:port]`. |
| `SLSKD_API_KEY` | yes, secret | Any long random string; slskd and Post-Vinyl share it. |
| `SLSKD_NETWORK_USERNAME`, `SLSKD_NETWORK_PASSWORD` | yes, secret | Your Soulseek login; the first login registers the name. |
| `SLSKD_UI_USERNAME`, `SLSKD_UI_PASSWORD` | yes, secret | slskd's own web UI login. |
| `PV_DATA_DIR` | no | Host folder for the database (default `./postvinyl_data`). |
| `PV_DEV` | no | `1` serves the API docs at `/docs`; leave empty in production. |
| `LISTENBRAINZ_TOKEN` | no | Navidrome's own ListenBrainz scrobbling. |
| `MUSICA_HTTP_PROXY`, `MUSICA_PROXY_*`, `NO_PROXY_LIST` | no | Optional SOCKS5 proxy for Navidrome. |

Inside the container the app's folders are `/postvinyl/data` and `/postvinyl/config`; the image
defaults `PV_DB_PATH` and `PV_CONFIG_PATH` to files in them.

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -v              # test suite
ruff check app/ && mypy app/  # lint + type check
```

## FAQ

**Do I need a ListenBrainz account for anything besides recommendations?**
No — search, download, MusicBrainz browsing, and Navidrome sync all work without one. Recommendations
require `LISTENBRAINZ_TOKEN`/`LISTENBRAINZ_USERNAME` in `.env`; the integration enables automatically
once both are set (restart required).

**Why does one search open thousands of connections?**
Soulseek search is broadcast network-wide — every peer with a match dials in, which is normal Soulseek
behavior, not a bug. Post-Vinyl rate-limits and caches searches to keep this manageable; see
[`docs/architecture.md#search-connection-behavior`](docs/architecture.md#search-connection-behavior).

**Does using Post-Vinyl mean I share files back to the Soulseek network?**
Yes. slskd shares `SLSKD_SHARED_DIR` (your Navidrome library folder) back to the Soulseek network by
default, the same as the official Soulseek client — you become a peer other users can download from,
not just a downloader. This is inherent to how the Soulseek network functions.

**Why do I need a proxy container?**
Only if ListenBrainz or Soulseek isn't reachable directly from where you host Post-Vinyl (e.g. a VPS in a
region without direct access). See [`docs/deployment.md`](docs/deployment.md) for when to use
`postvinyl-proxy` and when to skip it.

**Where do config settings actually live?**
Non-secret settings live in `config.toml` and are hot-reloadable from the UI (`POST /api/config`).
Secrets (passwords, tokens, API keys) live only in `.env` and require a restart.

**What happens to a download that fails to match anything?**
It's retried through a re-query "ladder" up to `download.max_retries_per_track`, then marked failed.
Manual downloads show the failure in the UI; recommendation downloads log it and move on — recs
failing occasionally is expected background attrition, not something that alerts you.

## known bugs

- If you see a download hang in queue forever ocasionally switching to "downloading", just cancel it, it should retry on its own

- You may want to clear slskd_config/incomplete from time to time to get rid of songs that errored in downloads

- If you notice your downloads fail in the logs and never start downloading due to the peers not responding after 5000ms, the main way that was found to counter this was increasing the `download.max_retries_per_track` value in config/config.toml or in the config UI

- 1 letter songs/albums (like "i" by Kendrick Lamar) are able to show up in the search results, but the soulseek search mechanism itself
really does not like those, so there is a high probability that a wrong track may be downloaded. You can try your hand at the search in the 
soulseek tab on the ui, but it is also likely to not show up :( This is what the upload feature will be aiming at, so you can 
source you music elsewhere and upload it to your server using the ui

## Planned

Not built yet, roughly in mind for later. Order and scope may change:

- **Frontend design** — the current UI is functional, not designed; a real visual pass is planned.
- **User-uploading** — adding tracks to the library directly instead of only via Soulseek/recs.
- **Built-in system notifications** — surfacing app/service issues (e.g. failed downloads, service
  outages) without having to watch the logs.
- **Multi-user support** — currently single-user (one Navidrome account, one ListenBrainz account).

## Acknowledgments

Post-Vinyl exists because of the ecosystem it sits on top of:

- **[slskd](https://github.com/slskd/slskd)** — the Soulseek client/daemon and REST API Post-Vinyl drives
  for all search and download work.
- **[Navidrome](https://www.navidrome.org/)** — the music server and Subsonic-API library/playlist
  backend Post-Vinyl reads and writes.
- **[beets](https://beets.io/)** — the tagging/organizing engine behind every automatic import.
- **[ListenBrainz](https://listenbrainz.org/)** — the open recommendation and scrobble-feedback service
  behind Comfort Zone, Fresh Picks, Deep Cuts, and love/hate sync.
- **[MusicBrainz](https://musicbrainz.org/)** — the open music metadata database behind canonical
  release resolution, browse/search, and beets' own tagging.
- The **Soulseek network and its users** — the actual source of every file Post-Vinyl downloads.

Special thanks to:

- **[Soulbeet](https://github.com/terry90/soulbeet)** - a different service for managing searches and downloads that i took a lot of inspiration from.
- Madvillainy by madlib and MF DOOM for being the perfect download test subject throughout the development process
- Macbook Air for not dying on me
- **ANYBODY** that reaches out (goobernation@duck.com) to report bugs and the general user experience. Know that it would mean a lot to me. You could also use issues to report stuff, highly appreciated as well!

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Forks, modifications, and redistribution are welcome;
see [`TRADEMARKS.md`](TRADEMARKS.md) for the (informal, non-legal) policy on the project name. No
warranty; you are responsible for compliance with the Soulseek network's terms and your local laws
regarding file sharing.
