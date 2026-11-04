# Self-hosting

Post-Vinyl is one container that plugs into a Navidrome and an slskd. The image is
`ghcr.io/goober-nation/post-vinyl:latest`, for AMD64 and ARM64. The
[project README](../README.md#self-hosting-with-docker) has the compose files for adding it to a
stack or starting from nothing, and the optional outgoing proxy.

## What you need

- Docker with Compose
- a Soulseek account (the first slskd login registers the name)
- a folder for your music, shared by all three containers

## Folders

```
/music                         one mount, the same in all three containers
├── library/                   Navidrome's music folder
└── downloads/complete/soulseek/   slskd's downloads folder
```

**Navidrome's music folder must be `library/` only**, so it never indexes half-finished downloads.
Moves between the two folders are renames, so keep them on one mount.

Other locations work: set them under `files` in `post-vinyl.yaml`. They must match your mounts, so
they can only be changed in the file, not in the app.

Post-Vinyl's own folders:

| Host | Container | Holds |
|---|---|---|
| `./postvinyl_config` | `/postvinyl/config` | `post-vinyl.yaml` and its backups |
| `./postvinyl_data` | `/postvinyl/data` | `post-vinyl.db`, `secret.key` |

The container starts as root, creates both folders, writes `post-vinyl.yaml` from the example if
it's missing, hands them to `PUID:PGID` (default 1000) and runs the app as that user. That user
also needs write access to the music folder.

## First start

Open the app on the port you published. On a new Navidrome, the login page offers **Create admin**;
after that, log in with your Navidrome account. A Navidrome admin is a Post-Vinyl admin.

Until someone creates that first admin, anyone who reaches the app or Navidrome can do so. Create
it right after starting, before exposing the app.

**One admin must have logged in** before files can be scanned and linked: background work reaches
Navidrome as the first admin with a working saved login.

## Behind a reverse proxy

Serve it over HTTPS. The session cookie is `Secure`, so browsers only send it over HTTPS or to
`localhost`. Plain HTTP on another address won't keep you logged in.

Set `PV_PUBLIC_ORIGIN` to the one address people use, such as `https://music.example.com`. Without
it, state-changing requests must match the `Host` header, so a proxy that rewrites `Host` gets
`403 cross-site request refused`.

## Backups

| What | Why |
|---|---|
| `postvinyl_data/post-vinyl.db` | Requests, history, pulls, settings. SQLite in WAL mode, so stop the app or use `sqlite3 .backup`; a plain copy of a running database can be inconsistent |
| `postvinyl_data/secret.key`, or `PV_SECRET_KEY` | Without it, every stored password and token is lost |
| `postvinyl_config/post-vinyl.yaml` | Your settings |

Keep the key apart from the database backup. If it's lost, users log in again and re-enter their
ListenBrainz tokens.

## Rotating the secret key

`PV_SECRET_KEY` takes one or more Fernet keys, comma-separated, newest first. New values are
encrypted with the first, and any listed key can decrypt. Logging in re-encrypts a user's Navidrome
password, but a ListenBrainz token is only re-encrypted when the user saves it again, so keep an
old key listed until those are done.

## Upgrades

```bash
docker compose pull
```

```bash
docker compose up -d
```

Database migrations run at start. A newer database refuses to start on an older app, so back up
before upgrading if you might go back. `latest` follows `main`, and each `v*` tag
publishes that version.

Images aren't pinned on purpose: a break shows what changed. These versions are known to work, so
go back to them if an upgrade breaks something:

| App | Known to work |
|---|---|
| slskd | 0.26.2 |
| Navidrome | 0.64.2 |
| Python (in the image) | 3.12 |
| SQLite | 3.38 or newer is required |

## Health and logs

`GET /api/system/ping` needs no login and talks to nothing, and the image's health check uses it.
It says the app is up, not that slskd or Navidrome are.

Logs go to the container's output. Successful polls are left out of the access log.

## Sharing back

slskd shares your library, as Soulseek expects. Search rate limits protect your account: see
[search limits](search-and-matching.md#limits-that-protect-the-account). You are responsible for
following the network's terms and your local laws.
