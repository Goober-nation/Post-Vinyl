# Deployment

Post-Vinyl ships as a Docker Compose stack (`docker-compose.yml`): `navidrome`, `slskd`, `postvinyl`, and an
optional `postvinyl-proxy`. `config/config.toml` ships in the repo/image already set to sane defaults —
you don't need to copy it. `.env.example` at the repo root is the authoritative template for secrets;
`config.toml.example` documents every `config/config.toml` field with an explanatory comment — keep
both in sync if you add a setting.

## First-run checklist

1. Run `./setup.sh` from the repo root. It creates `.env` from `.env.example` if missing, then checks
   `MUSIC_HOST_DIR` — if it's still blank or the `/path/to/host/music` placeholder, it **stops right
   there** and asks you to edit `.env` first, before touching anything else. `setup.sh` never guesses
   this value: Docker silently creates and mounts an empty directory for any bind-mount path that
   doesn't exist, so a wrong or placeholder `MUSIC_HOST_DIR` fails quietly instead of loudly — better to
   stop and ask.
2. Edit `.env` and set `MUSIC_HOST_DIR` to your actual music library path on this host.
3. Run `./setup.sh` again. With `MUSIC_HOST_DIR` set, it now generates a random `SLSKD_API_KEY` if one
   isn't already set, and copies `slskd.yml.example` to `slskd_config/slskd.yml` as a reference (slskd
   itself is configured via `docker-compose.yml`'s env vars, not this file, until you turn on
   `remote_configuration` to edit it directly). It does **not** start the stack itself — it prints
   `docker compose up -d` for you to run when ready. `SLSKD_API_KEY` specifically **must** be generated
   before the stack starts — Docker Compose substitutes `${VARS}` from `.env` once, at parse time,
   before any container starts, so setting it any later (an init container, musica's own entrypoint) is
   always one run too late.
4. `MUSICA_AUTH_USERNAME` / `MUSICA_AUTH_PASSWORD` (optional HTTP Basic Auth over the whole app) is the
   one value setup.sh and the in-app wizard don't cover — edit it into `.env` yourself if you want it;
   leave both blank only for a trusted single-machine/LAN deployment. Everything else
   (`NAVIDROME_USERNAME/PASSWORD`, `SLSKD_NETWORK_USERNAME/PASSWORD`, `LISTENBRAINZ_TOKEN/USERNAME`) is
   handled by the in-app setup wizard in step 6 below — you don't need to touch `.env` for those unless
   you'd rather set them by hand before first boot.
5. Start the stack: `docker compose up -d`.
6. Open Post-Vinyl's panel (`http://localhost:8092`). A setup wizard opens automatically on first visit
   (unless every account is already configured) and walks through:
   - **Navidrome** — creates the first admin account via Navidrome's own `POST /auth/createAdmin` (or
     verifies+saves credentials if an admin already exists), then saves them to `.env`. Navidrome shows
     "disabled" in service health until the app restarts and picks up the new credentials — the wizard
     step has a "Restart app now" button for exactly this (no separate Docker action needed; it's the
     same self-restart the Config tab's "Restart app" button uses).
   - **Soulseek** — saves a chosen username/password to `.env`. **Other Soulseek users will see this
     username** — it's your real network identity, not an app login. Distinct from
     `SLSKD_WEBUI_USERNAME/PASSWORD` (slskd's own local admin panel login, commented out by default,
     unrelated to Soulseek). slskd needs a manual `docker compose up -d slskd` to pick it up — Post-
     Vinyl has no Docker socket access to trigger that itself (a deliberate design choice; see
     [api.md](api.md#setup)). **Don't use `docker compose restart slskd`** — restart keeps the
     container's existing environment instead of re-reading `.env`, so the new login is silently
     ignored and slskd never connects; `up -d` is what makes Compose re-resolve the env vars and
     recreate the container — after which "Check connection" reports whether it connected or the
     username's already taken by someone else.
   - **ListenBrainz** (optional) — a link to get a token plus fields to save it; skip if you don't want
     recommendations yet.
   
   Every step is skippable and can be revisited later via **Config → Re-run setup**
   (`POST /api/setup/rerun`). `GET /api/system/status` shows current reachability for all three
   services at any point.
7. If ListenBrainz isn't directly reachable (see [Proxy](#proxy--vpn) below), configure `postvinyl-proxy`
   before expecting recommendations to populate.

## Ports

| Service | Container port | Host port (default) |
|---|---|---|
| Navidrome | 4533 | 8090 |
| slskd web UI | 5030 | 8091 |
| slskd Soulseek listen (TCP) | 50300 | 50300 |
| slskd Soulseek listen (UDP) | 50305 | 50305 |
| postvinyl | 8000 | 8092 |

The Soulseek listen ports (50300/tcp, 50305/udp) should be forwarded on your router/firewall if you
want inbound peer connections — Soulseek works without this, but with reduced download availability
since some peers require a direct connection.

## Proxy / VPN

`postvinyl-proxy` (an HTTP→SOCKS5 bridge, `ginuerzh/gost`) exists for one situation: **Post-Vinyl's outbound
HTTP calls to ListenBrainz or Soulseek aren't reachable directly from wherever you're running the
stack** — most commonly a VPS in a region ListenBrainz/Soulseek restrict, or a host where you route
select traffic through a SOCKS5 VPN endpoint.

`MUSICA_HTTP_PROXY` is the on/off switch, checked by `postvinyl`'s `http_proxy`/`https_proxy` env vars
in `docker-compose.yml`. Leave it blank (the `.env.example` default) and every outbound call goes out
directly, no `postvinyl-proxy` involved — this is the right setting for most deployments, including
ones that never touch the `postvinyl-proxy` service at all. Only set it to
`http://postvinyl-proxy:8080` once you've also filled in the SOCKS5 fields below; setting it while
they're blank points every outbound request at a proxy with nowhere to go, and everything — including
MusicBrainz search, which never needs a proxy — fails with a 503.

If you do need it: set `MUSICA_HTTP_PROXY=http://postvinyl-proxy:8080` and point
`MUSICA_PROXY_SOCKS_HOST`/`MUSICA_PROXY_SOCKS_PORT`/`MUSICA_PROXY_USER`/`PASS` at your SOCKS5 endpoint.
`postvinyl-proxy` only proxies the `postvinyl` container's `http_proxy`/`https_proxy` env vars —
**slskd is not proxied through it**; Soulseek's protocol isn't standard HTTP, so slskd needs its own
direct network path (or its own VPN routing at the container/host level) if Soulseek itself is blocked
from your host. `NO_PROXY_LIST` must include the in-stack hostnames plus `musicbrainz.org`
(`localhost,127.0.0.1,navidrome-server,slskd,postvinyl,musicbrainz.org`, the `.env.example` default) so
container-to-container calls and MusicBrainz — which is globally reachable and never needs a proxy —
don't get routed through it.

Changing `MUSICA_HTTP_PROXY` or `NO_PROXY_LIST` only takes effect after recreating the `postvinyl`
container (`docker compose up -d postvinyl`), not a plain restart — both are Compose `${VAR}`
substitutions baked in at container creation, same as `SLSKD_API_KEY`.

Note the connection-volume behavior in [architecture.md#search-connection-behavior](architecture.md#search-connection-behavior):
a VPN or proxy in the path for slskd's peer connections will see the same multi-thousand-socket burst
per search that the host does — a low-throughput or per-connection-billed VPN tunnel may struggle here
regardless of Post-Vinyl's own rate limiting, since the volume is real Soulseek network behavior, not
excess traffic Post-Vinyl generates.

## Remote access (Tailscale)

For accessing Post-Vinyl's panel or Navidrome from outside your LAN without exposing ports publicly,
[Tailscale](https://tailscale.com/) is a low-effort option: install the Tailscale client on the Docker
host, and reach the stack at its Tailscale IP/hostname on the same ports listed above — no
`docker-compose.yml` changes needed, since Tailscale operates at the host network layer. Combine with
`MUSICA_AUTH_USERNAME`/`PASSWORD` if the Tailscale network includes devices you don't fully trust, or if
you also expose the stack another way. Tailscale's own MagicDNS makes `http://postvinyl-host:8092` usable
from any device on the tailnet without remembering an IP.

This is deliberately not a Post-Vinyl-specific integration — the stack has no Tailscale-aware code, it's
purely a networking layer in front of the same ports.

## Config drift

`config.toml.example`, `.env.example`, and the shipped `config/config.toml` must all reflect the same
full set of fields: every setting the app reads should exist in `config.toml.example` with an
explanatory comment, and in `config/config.toml` with its shipped default. If you add a new config
field in code without updating both, a fresh setup silently misses it. There's no automated check for
this yet — until then, treat "new config field added" and "both templates updated" as one change, not
two.

## Updating

```bash
git pull
docker compose build postvinyl
docker compose up -d
```

Database migrations in `app/db/migrations/` run automatically on startup (`Database.initialize_schema()`),
tracked in an `applied_migrations` table — no manual migration step needed.

## Troubleshooting

- **Everything feels slow/stuck after a burst of searches**: this is very likely socket saturation from
  Soulseek's broadcast search behavior, not a hang — see
  [architecture.md#search-connection-behavior](architecture.md#search-connection-behavior). Give it a
  minute; it self-clears. Also check `download.history_clear_interval_minutes` isn't disabled (`0`),
  since accumulated slskd transfer history has separately caused stack congestion.
- **`docker compose` commands themselves feel slow**: same root cause on Docker Desktop — the host's
  userspace port-forwarder is shared across every published port, so it stalls alongside the app even
  though the app itself is still responding quickly inside its container.
- **Downloads import with wrong tags**: check `docs/architecture.md`'s Import pipeline section — MBID
  resolution has known edge cases with weak artist discrimination on title-only matches.
- **Recommendations aren't populating**: confirm both `LISTENBRAINZ_TOKEN` and `LISTENBRAINZ_USERNAME`
  are set (both required) and that `GET /api/system/status` shows ListenBrainz reachable; check the
  proxy section above if you're on a VPS.
- **Config change didn't take effect**: secrets (`.env`) always require a restart; everything in
  `config.toml` should hot-reload via `POST /api/config` — if it didn't, check `GET /api/logs` for a
  config validation error.
- **Setup wizard says "saved" but the credentials don't survive a restart/rebuild**: confirm
  `docker-compose.yml`'s `postvinyl` service has `./.env:/app/.env` in its `volumes:`. Without it, the
  wizard (and `POST /api/config/secrets`) still write successfully, just into the container's own
  throwaway filesystem instead of the host's `.env` — the write "succeeds" but is invisible outside
  that one container and gone on the next rebuild.
