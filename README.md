# Post-Vinyl

[![GitHub License](https://img.shields.io/github/license/Goober-nation/Post-Vinyl)](LICENSE)
[![GitHub Repo stars](https://img.shields.io/github/stars/Goober-nation/Post-Vinyl)](https://github.com/Goober-nation/Post-Vinyl)

Post-Vinyl is a self-hosted music downloader and library manager. Search for a song, album or artist, or pull your ListenBrainz recommendations, and it finds the right file on Soulseek, tags it, files it and adds it to your Navidrome library.

## Features

- **Search & Download**: Search MusicBrainz and Deezer for songs, albums and artists, fill a cart, and download. Post-Vinyl picks the peer and checks the file really is the song you asked for.
- **Careful Matching**: Live versions, covers, remixes, karaoke and the like are skipped unless that's what you asked for.
- **ListenBrainz Recommendations**: Keep each of your 1000-song recommendation lists, choose a range, preview the songs, and pull them into a named playlist.
- **ListenBrainz Radio**: Pick an artist and a level (easy, medium or hard), reroll until you like it, and pull it into a playlist.
- **Your Requests Go First**: Pulls download in the background, behind anything you ask for yourself. Cancel one at any time.
- **Honest Tags**: Tags come from MusicBrainz and ListenBrainz lookups only, with no guessing.
- **Soulseek Tab**: Search Soulseek directly and pick the exact files yourself.
- **File Management**: Browse, rename and delete files in your library.
- **Multi-user**: Log in with your Navidrome account. Each user has their own requests, sources, settings and ListenBrainz token.

## How It Works

1. **Post-Vinyl** turns your request into a real song through MusicBrainz or Deezer and searches Soulseek through **slskd**.
2. It ranks the peers, downloads the best match, verifies the file, tags it and moves it into your library.
3. **Navidrome** indexes the song, ready to stream from any Subsonic app. Songs from a pull also join that pull's playlist.

For listening I use the Navidrome web app or [Feishin](https://github.com/jeffvli/feishin/releases) on a computer, and [Arpeggi](https://github.com/argie-w/Arpeggi-App) on iOS.

## Self-Hosting with Docker

Post-Vinyl ships as a Docker Compose stack: the app, Navidrome and slskd.

### Prerequisites

- Docker with Compose 2.24 or newer
- A music folder containing the empty folders `library/` and `downloads/complete/soulseek/`. slskd won't start without the downloads folder.

### Quick Start

The same commands work in bash, PowerShell and the macOS terminal.

1. Copy `.env.example` to `.env` and fill it in (see [Environment Variables](#environment-variables)). Generate `PV_SECRET_KEY` once:

   ```bash
   docker compose run --rm --no-deps postvinyl python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Back the key up outside the database. Losing it loses every stored secret.

2. Create the first admin, which is both the Navidrome and the Post-Vinyl admin. Navidrome stays unpublished while this runs:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.setup.yml run --rm setup
   ```

   ```bash
   docker compose down
   ```

3. Start the stack:

   ```bash
   docker compose up -d
   ```

### Initial Setup

1. Open `http://localhost:8092` and log in with your Navidrome account.
2. Search for something and download it.
3. For recommendations and radio, add your ListenBrainz token in **Settings**.

| Service | Default address |
|---|---|
| Post-Vinyl | `http://localhost:8092` |
| Navidrome | `http://localhost:8090` |
| slskd | `http://localhost:8091` |

## Configuration

Secrets live in `.env`. Everything else lives in `postvinyl_config/post-vinyl.yaml`, created from [`post-vinyl.example.yaml`](post-vinyl.example.yaml) on first start, with each value explained inside. Admins can also change it under **Settings > Server-wide**. Restart after a change.

Settings under **Settings > Mine** apply only to you, and take effect at once.

### Environment Variables

| Variable | Needed | What it is |
|---|---|---|
| `MUSIC_HOST_DIR` | yes | Your music folder. Navidrome's library is its `library/` subfolder. |
| `SLSKD_DOWNLOADS_DIR` | yes | Where slskd saves downloads inside `/music` (default in `.env.example`). |
| `PV_SECRET_KEY` | yes, secret | Encrypts stored per-user secrets. |
| `SLSKD_API_KEY` | yes, secret | Any long random string; slskd and Post-Vinyl share it. |
| `SLSKD_NETWORK_USERNAME`, `SLSKD_NETWORK_PASSWORD` | yes, secret | Your Soulseek login, also used for slskd's web UI; the first login registers the name. |
| `PV_PUBLIC_ORIGIN` | no | The one address people open the app at, `scheme://host[:port]`. Stricter protection against forged requests; set it when the app is public, or if your reverse proxy changes the `Host` header. |
| `PV_DATA_DIR` | no | Host folder for the database (default `./postvinyl_data`). |
| `PUID`, `PGID` | no | The user and group Post-Vinyl runs as (default `1000`). |
| `PV_DEV` | no | `1` serves the API docs at `/docs`; leave empty in production. |
| `LISTENBRAINZ_TOKEN` | no | Navidrome's own ListenBrainz scrobbling. |
| `MUSICA_HTTP_PROXY`, `MUSICA_PROXY_*`, `NO_PROXY_LIST` | no | Optional SOCKS5 proxy. |

## FAQ

**Do I need a ListenBrainz account?**
Only for recommendations and radio. Search, download and everything else work without one.

**Do I share files back to Soulseek?**
Yes. slskd shares your library, the same as the official client. Soulseek only works because everyone shares back.

**Why does one search open thousands of connections?**
Soulseek searches are broadcast, so every peer with a match connects to you. That's normal. Post-Vinyl limits how often it searches, because searching too fast gets the account throttled.

**How long does a big pull take?**
Hours. Searches are limited to 4 a minute, so 1000 songs take over 4 hours at best.

**What happens when a download fails?**
Post-Vinyl re-reads the same search and tries the next peer, up to `slskd.candidates_per_download` peers, then marks the request failed. Asking for the song again starts a fresh search.

## Known Bugs

- You may want to clear `slskd_config/incomplete` from time to time to remove songs that failed mid-download.
- One-letter titles (like "i" by Kendrick Lamar) show up in search, but Soulseek's search handles them badly, so a wrong track may be downloaded. Try the Soulseek tab, though it may not show up there either.

## Planned

Order and scope may change.

- Approval inbox and Postradio recommendations
- Yandex playlists
- Custom and smart playlists
- Uploading your own files
- Notifications for failed downloads and service outages

## API

Every endpoint is listed in [`docs/api.md`](docs/api.md).

## Development

```bash
pip install -r requirements.txt
```

```bash
pytest tests/ -v
```

```bash
ruff check app/ && mypy app/
```

## Acknowledgments

- **[slskd](https://github.com/slskd/slskd)**: the Soulseek client behind every search and download.
- **[Navidrome](https://www.navidrome.org/)**: the music server.
- **[ListenBrainz](https://listenbrainz.org/)**: recommendations and radio.
- **[MusicBrainz](https://musicbrainz.org/)**: song data and tags.
- **[Deezer](https://www.deezer.com/)**: song data and covers.
- The **Soulseek network and its users**: the source of every file.

Special thanks to:

- **[Soulbeet](https://github.com/terry90/soulbeet)**, a different service for managing searches and downloads that I took a lot of inspiration from.
- **[Yuzic](https://github.com/eftpmc/yuzic)**, for attempting slskd downloading directly in a mobile app.
- Madvillainy by Madlib and MF DOOM, for being the perfect download test subject.
- MacBook Air, for not dying on me.
- **Anybody** who reaches out (goobernation@duck.com) to report bugs or share how it's going. It would mean a lot.

## License

Apache License 2.0, see [`LICENSE`](LICENSE). Forks, modifications and redistribution are welcome; see [`TRADEMARKS.md`](TRADEMARKS.md) for the informal policy on the name. No warranty. You are responsible for following the Soulseek network's terms and your local laws on file sharing.
