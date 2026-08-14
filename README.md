# Post-Vinyl v0.1 🎉

## TL;DR

Post-Vinyl is a self-hosted music library that fills itself in. Point it at a MusicBrainz search (or
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
   recommendations, in its category playlist — ready to stream from any Subsonic-compatible app.

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

1. Run the setup script:
   ```bash
   ./setup.sh
   ```
   It creates `.env` from the template, then stops and asks you to set `MUSIC_HOST_DIR` to your real
   music library path — it never guesses this, since Docker will silently mount an empty directory for
   a bind-mount path that doesn't exist. Edit `.env` and run `./setup.sh` again; it'll then generate
   slskd's API key and print the next command. `config/config.toml` already ships with sane defaults,
   so nothing else needs copying.
2. Start the stack:
   ```bash
   docker compose up -d
   ```
3. Open `http://localhost:8092`. A setup wizard opens automatically on first visit and walks you
   through the rest:
   - **Navidrome** — enter a username/password; the wizard creates the admin account (or verifies it,
     if one already exists) and saves the credentials. Navidrome shows as "disabled" in service health
     until the app restarts and picks them up — the wizard offers a "Restart app now" button right
     there so you don't have to go find it in Config.
   - **Soulseek** — enter a username/password for slskd to log into the Soulseek network with (logging
     in for the first time *is* how a new username registers — there's no separate signup). Saving
     needs one manual `docker compose up -d slskd` (Post-Vinyl has no Docker access to do this
     itself — and it must be `up -d`, not `restart`: restart keeps the container's old environment
     instead of re-reading `.env`, so the new login silently never takes effect), then "Check
     connection" tells you if it connected or if that username's already taken by someone else.
   - **ListenBrainz** (optional) — a link to get a token, plus fields to save it. Skip it if you don't
     want recommendations yet; you can fill this in later.
   
   Every step can be skipped and revisited later from **Config → Re-run setup**. After the wizard, a
   short tutorial overlay points out what each tab does.

See [`docs/deployment.md`](docs/deployment.md) for what each `.env` value means, VPN/proxy notes,
remote-access guidance (Tailscale), and troubleshooting.

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
Secrets (passwords, tokens, API keys) live only in `.env` and require a restart. Never put a secret in
`config.toml`.

**What happens to a download that fails to match anything?**
It's retried through a re-query "ladder" up to `download.max_retries_per_track`, then marked failed.
Manual downloads show the failure in the UI; recommendation downloads log it and move on — recs
failing occasionally is expected background attrition, not something that alerts you.

## known bugs

- If you see a download hang in queue forever ocasionally switching to "downloading", just cancel it, it should retry on its own

- You may want to clear slskd_config/incomplete from time to time to get rid of songs that errored in downloads

- If you notice your downloads fail in the logs and never start downloading due to the peers not responding after 5000ms, the main way that was found to counter this is increasing the download.max_retries_per_track value in config/config.toml

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
- **[Soulbeet](https://github.com/terry90/soulbeet)** - a different service for managing searches and downloads that i took a lot of inspiration from.
- The **Soulseek network and its users** — the actual source of every file Post-Vinyl downloads.
- Madvillainy by madlib and MF DOOM for being the perfect test subject throughout the development process
- Macbook Air for not dying on me

- **ANY** of you people that may [reach out](goobernation@duck.com) to me to report bugs and their general user experience, that would mean a lot to me

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). Forks, modifications, and redistribution are welcome;
see [`TRADEMARKS.md`](TRADEMARKS.md) for the (informal, non-legal) policy on the project name. No
warranty; you are responsible for compliance with the Soulseek network's terms and your local laws
regarding file sharing.
