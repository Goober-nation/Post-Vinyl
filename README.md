# Post-Vinyl

<img src="web/src/logo.svg" alt="Post-Vinyl logo" width="128">

[![Build](https://img.shields.io/github/actions/workflow/status/Goober-nation/Post-Vinyl/image.yml?branch=main&label=build)](https://github.com/Goober-nation/Post-Vinyl/actions/workflows/image.yml)
[![Version](https://img.shields.io/github/v/release/Goober-nation/Post-Vinyl?label=version)](https://github.com/Goober-nation/Post-Vinyl/releases/latest)
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
- **Multi-user**: Log in with your Navidrome account. Each user has their own requests, sources, settings and ListenBrainz token. Admins can add users, change passwords and delete users from the Admin tab.

## How It Works

1. **Post-Vinyl** turns your request into a real song through MusicBrainz or Deezer and searches Soulseek through **slskd**.
2. It ranks the peers, downloads the best match, verifies the file, tags it and moves it into your library.
3. **Navidrome** indexes the song, ready to stream from any Subsonic app. Songs from a pull also join that pull's playlist.

For listening I use the Navidrome web app or [Feishin](https://github.com/jeffvli/feishin/releases) on a computer, and [Arpeggi](https://github.com/argie-w/Arpeggi-App) on iOS.

## Self-Hosting with Docker

Post-Vinyl is one container that plugs into a Navidrome and an slskd. The image is `ghcr.io/goober-nation/post-vinyl:latest`, for AMD64 and ARM64.

### Quick Start

Already running Navidrome and slskd? Add this service to your stack:

```yaml
postvinyl:
  image: ghcr.io/goober-nation/post-vinyl:latest
  restart: on-failure:3
  ports:
    - "8092:8000"
  environment:
    - PV_NAVIDROME_URL=http://navidrome:4533
    - PV_SLSKD_URL=http://slskd:5030
    - PV_SLSKD_API_KEY=change-me # the API key from your slskd config
  volumes:
    - /path/to/music:/music # the same folder Navidrome and slskd use
    - ./postvinyl_data:/postvinyl/data
    - ./postvinyl_config:/postvinyl/config
```

Your music folder needs `library/` (Navidrome's music folder) and `downloads/complete/soulseek/` (where slskd saves). Different folders are fine: set them under `files` in `post-vinyl.yaml`.

### Starting From Nothing

Create the two folders above, then fill in the `change-me` values and paths in this `docker-compose.yml`:

```yaml
services:
  navidrome:
    image: deluan/navidrome:latest
    restart: unless-stopped
    ports:
      - "8090:4533"
    environment:
      - ND_MUSICFOLDER=/music/library
      - ND_DATAFOLDER=/data
      - ND_SCANNER_PURGEMISSING=never
    volumes:
      - /path/to/music:/music:ro
      - ./navidrome_data:/data

  slskd:
    image: slskd/slskd:latest
    restart: unless-stopped
    user: "1000:1000"
    ports:
      - "8091:5030"
      - "50300:50300/tcp"
    environment:
      # Your Soulseek login; the first login registers the name
      - SLSKD_SLSK_USERNAME=change-me
      - SLSKD_SLSK_PASSWORD=change-me
      - SLSKD_USERNAME=change-me # slskd's web UI login
      - SLSKD_PASSWORD=change-me
      - SLSKD_API_KEY=change-me-any-long-random-string
      - SLSKD_SHARED_DIR=/music/library
      - SLSKD_DOWNLOADS_DIR=/music/downloads/complete/soulseek
      - SLSKD_REMOTE_CONFIGURATION=true
      - SLSKD_SLSK_LISTEN_PORT=50300
    volumes:
      - /path/to/music:/music
      - ./slskd_config:/app
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:5030/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s

  postvinyl:
    image: ghcr.io/goober-nation/post-vinyl:latest
    restart: on-failure:3
    ports:
      - "8092:8000"
    environment:
      - PV_NAVIDROME_URL=http://navidrome:4533
      - PV_SLSKD_URL=http://slskd:5030
      # The same as SLSKD_API_KEY
      - PV_SLSKD_API_KEY=change-me-any-long-random-string
    volumes:
      - /path/to/music:/music
      - ./postvinyl_data:/postvinyl/data
      - ./postvinyl_config:/postvinyl/config
    depends_on:
      navidrome:
        condition: service_started
      slskd:
        condition: service_healthy
```

```bash
docker compose up -d
```

### Optional: Proxy

You only need this if ListenBrainz or MusicBrainz can't be reached from where you host Post-Vinyl. It sends Post-Vinyl's own requests through a SOCKS5 proxy. Add the service below with your proxy's details, and add the environment lines to the `postvinyl` service:

```yaml
services:
  postvinyl-proxy:
    image: ginuerzh/gost:latest
    restart: unless-stopped
    command: "-L=http://:8080 -F=socks5://user:password@host:port"

  postvinyl:
    environment:
      - HTTP_PROXY=http://postvinyl-proxy:8080
      - HTTPS_PROXY=http://postvinyl-proxy:8080
      - NO_PROXY=localhost,127.0.0.1,navidrome,slskd
```

### Initial Setup

1. Open `http://localhost:8092`. On a new Navidrome, create its admin right there; after that, log in with your Navidrome account.
2. A Navidrome admin is a Post-Vinyl admin.
3. Search for something and download it.
4. For recommendations and radio, add your ListenBrainz token in **Settings**.

## Configuration

Everything is optional from here. `postvinyl_config/post-vinyl.yaml` is created on first start with each value explained inside. Admins can also change it under **Settings > Server-wide**, then restart. **Settings > Personal** applies only to you, at once.

Optional environment values on the `postvinyl` service:

| Value | What it does |
|---|---|
| `PV_SECRET_KEY` | Encrypts stored passwords and tokens. Unset, a key is created at `postvinyl_data/secret.key`. Back that folder up: losing the key loses every stored secret. |
| `PV_PUBLIC_ORIGIN` | The one address people open the app at, `scheme://host[:port]`. Stricter protection against forged requests. Set it when the app is public, or if your reverse proxy changes the `Host` header. |
| `PUID`, `PGID` | The user and group Post-Vinyl runs as (default `1000`). |
| `PV_DEV` | `1` serves the API docs at `/docs`. |

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

## Documentation

Guides, configuration, architecture and the API are in [`docs/`](docs/README.md).

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

- **[Soulbeet](https://github.com/terry90/soulbeet)**, a different service for managing searches and downloads that I took a lot of inspiration from. Shoutout to Terry.
- **[Yuzic](https://github.com/eftpmc/yuzic)**, for attempting slskd downloading directly in a mobile app.
- Madvillainy by Madlib and MF DOOM, for being the perfect download test subject.
- MacBook Air, for not dying on me.
- **Anybody** who reaches out (timofei.ruchkin.dev@gmail.com) to report bugs or share how it's going. It would mean a lot.

## License

Apache License 2.0, see [`LICENSE`](LICENSE). Forks, modifications and redistribution are welcome; No warranty. You are responsible for following the Soulseek network's terms and your local laws on file sharing.
