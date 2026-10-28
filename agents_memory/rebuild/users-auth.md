# Users & auth: plan (decided 2026-10-25, not implemented)

Replaces today's HTTP Basic auth (`MUSICA_AUTH_USERNAME/PASSWORD`, `WWW-Authenticate`
header in `app/main.py`). The old `users`/`sessions` tables were dropped in migration 014
(never wired), so this starts fresh as migration **016**.

## Requirements (user)
- App is on a public subdomain behind HTTPS. Auth required everywhere.
- Login is a page inside the app. No browser popup, no `WWW-Authenticate` header.
- Sessions: **30-day sliding expiry**, renewed on use.
- No public sign-up. Accounts are created by an admin **in Navidrome**.
- Login rate-limited; account locks briefly after repeated failures.
- **Default deny**: every route, including the SSE event stream, needs a session unless
  it's on an explicit allowlist.
- Cookies HttpOnly, Secure, SameSite=Lax. State-changing requests need a CSRF check.
- Losing the encryption key loses every stored secret, so back the key up outside the DB.

## Decided 2026-10-28 (milestone 4 planning; wins over the plan below)
- Credentials are checked with the Navidrome client's `login()` (`POST /auth/login`, #80),
  not Subsonic `ping`; it also returns the JWT and a Subsonic token for the session's calls.
- **The encrypted Navidrome password is still stored** (re-encrypted on each login): background
  native-API work must renew the 48 h JWT without the user present.
- **Lockout is in memory too** (user, 2026-10-28): failed-login counts and lock expiry live in
  the auth service, not in `users` (no `failed_logins` / `locked_until` columns). A restart
  clears them; accepted, as they reset after minutes anyway.
- **Per-IP login limit: a small in-memory limiter**, not `slowapi` (no new dependency; one
  app process).
- **Encryption is built** (#85): `app/services/encryption.py` (named so it doesn't shadow the
  stdlib `secrets` the auth service needs). `SecretBox.from_env()` reads `PV_SECRET_KEY`:
  one or more Fernet keys, **comma-separated, newest first**. Encrypts with the first, decrypts
  with any; `rotate()` re-encrypts under the newest so an old key can then be dropped. A
  missing/invalid key raises `SecretKeyError` (refuse to start); a wrong key or altered value
  raises `DecryptError` (never a silent empty value). `generate_key()` is for the setup wizard.
  Dependency: `cryptography>=44`.
- **Auth service is built** (#87): `app/services/auth.py`, `AuthService` behind
  `AuthServiceBase`. Service only; the cookie, default-deny middleware, CSRF check and
  login/logout routes come with the app skeleton (#88) (user, 2026-10-28).
- **Failed logins: one server-wide cap** (user, 2026-10-28; replaces the per-account
  10 → 5 min lock and the per-IP limit): **20 failed logins in an hour lock every login until
  that hour's window ends**. In memory, fixed window; a success doesn't reset it; Navidrome
  being down doesn't count. Accepted downside: anyone can lock everyone out for up to an hour.
  Wrong password and unknown user give the same error. The lock only blocks new logins: running
  sessions keep working (user, 2026-10-28).
- **Session touch at most hourly** (user, 2026-10-28): `authenticate()` pushes `expires_at` to
  now + 30 days only when `last_seen_at` is an hour old; expired rows are deleted when met.
- The stored username is Navidrome's spelling (from its login response), not the typed one.
- **App skeleton is built** (#88): `app/main.py` (`create_app`, run with
  `uvicorn app.main:create_app_from_env --factory`), `app/config.py`, `app/middleware.py`,
  `app/dependencies.py`, `routes/auth.py` (`POST /api/auth/login`, `POST /api/auth/logout`,
  `GET /api/auth/me`), `routes/system.py` (`GET /api/system/ping`).
  - **Config** (user, 2026-10-28): env vars read into one `Settings` dataclass until the
    config redesign: `PV_DB_PATH`, `PV_NAVIDROME_URL`, `PV_PUBLIC_ORIGIN`
    (scheme://host[:port]), `PV_DEV` (serves `/docs`; off by default). A missing one refuses
    to start.
  - **Default deny**: everything returns 401 without a valid `pv_session` cookie, except
    `POST /api/auth/login`, `GET /api/system/ping`, and `/docs` + `/openapi.json` in dev.
    Unknown paths also return 401 (deny before 404).
  - **CSRF** (user, 2026-10-28): POST/PUT/PATCH/DELETE need `Origin` equal to
    `PV_PUBLIC_ORIGIN`, else `Referer`'s origin; neither → 403. Login is checked too.
  - Cookie: HttpOnly, Secure, SameSite=Lax, Path=/, Max-Age 30 days. Login errors: 401 wrong
    credentials, 429 + `Retry-After` when locked, 503 when Navidrome is down.
- **No setup endpoint; Navidrome is never empty** (user, 2026-10-28; **interim**: the user finds
  it suboptimal and prefers setup through the app or a setup CLI script; decided in milestone 6,
  see `work-plan.md`, "To decide in milestone 6"). An empty Navidrome lets
  anyone who reaches it call `createAdmin`, so the deployment sets
  `ND_DEVAUTOCREATEADMINPASSWORD` and Navidrome creates its admin on first start. Post-Vinyl
  never calls `createAdmin`; the first admin is simply the first Navidrome admin to log in
  (`is_admin` is copied). Checked in the v0.64.2 source (`server/initial_setup.go`):
  - it runs only during Navidrome's initial setup (a fresh data folder), creating user
    `admin` with that password if missing;
  - **it logs the password in plain text** at warn level, so change the admin password
    in Navidrome after the first start (the log then shows a dead password);
  - it is labelled a dev option: pin the Navidrome image and re-check it on upgrades.
  - **To do in milestone 7 (wiring):** add it to `docker-compose.yml` from `.env`, pin the
    Navidrome image (it's `:latest` today), and verify on a throwaway Navidrome.
- The new app never had HTTP Basic auth, so "remove Basic auth" is only about `legacy/`.

## Decisions
- **Password checked against Navidrome on every login** (Subsonic `ping` with the typed
  credentials). Accepted tradeoff: PV login fails while Navidrome is down.
- **Every PV user = a Navidrome user.** The admin creates users in the ND UI; a user's
  first successful PV login creates their PV row (`is_admin` copied from ND).
- PV creates an ND user only once: the first-ever ND admin in the setup wizard.
  Detection is by trying: `POST /auth/createAdmin` succeeds only while ND has zero users
  (200 = created), 403 = an admin exists → verify credentials via `ping`. The wizard
  already does this (`app/routes/setup.py: setup_navidrome`).
- PV never edits or deletes ND users.
- ND password stored **encrypted** per user (Fernet), re-encrypted from the typed
  password on every successful login, so ND password changes take effect at next login.
- **Lockout: 10 failed logins → 5 min lock** (per account), plus a per-IP rate limit on
  the login route.
- **ListenBrainz token**: per user, entered by each user **in the app**. Do **not** code
  any migration of `LISTENBRAINZ_TOKEN` from `.env`; the user removes it from `.env`
  by hand later.
- **HTTP Basic auth is removed in the same change** (both active = popup + login page).
- Single user exposed in the UI for the MVP; schema supports several. `requests.user_id` is a
  real FK to `users` (decided 2026-10-25).
- 2FA (TOTP, e.g. `pyotp`): recorded as an option, skipped for the MVP.

## Build plan (one change, in backlog2 phase 1 with the DB work; decided 2026-10-25)
1. **`app/services/secrets.py`**: `cryptography.Fernet` wrapped in `MultiFernet` (key
   rotation); key in `.env` as `PV_SECRET_KEY`. The wizard generates it when missing and
   tells the user to back it up.
2. **Migration 016**
   - `users`: id, `username` (unique, `COLLATE NOCASE`, same as the ND username),
     `nd_password_enc`, `lb_token_enc` (NULL until set in the app), `is_admin`,
     `status` (`active` / `needs_reauth`), `failed_logins`, `locked_until`, `created_at`.
   - `sessions`: id, `user_id` (FK), `token_hash` (SHA-256 of the cookie's random token;
     the raw token is never stored), `created_at`, `last_seen_at`, `expires_at`.
3. **Login/logout**: login page; `POST /api/auth/login` verifies against ND, creates the
   PV row on first login, re-encrypts the password, applies the lockout, sets the session
   cookie; `POST /api/auth/logout` deletes the session. Per-IP limit via `slowapi`.
4. **Default deny**: middleware rejects requests without a valid session. Allowlist:
   login page + route, static files, `/api/system/ping` (healthcheck), the setup wizard
   **only while no admin exists**. POST/PUT/PATCH/DELETE require an `Origin` header
   matching the app's own origin (CSRF). `current_user` dependency for routes.
5. **Setup wizard creates the first admin**: until a PV admin exists the app serves only
   the wizard; the ND step creates/verifies the ND admin as today, then creates the
   matching PV admin row and logs in.
6. **Remove HTTP Basic auth** and the `MUSICA_AUTH_*` settings (`app/main.py`,
   `app/config.py` `AuthConfig`, `.env.example`, docs).

## Open (not decided)
- **Token input UI**: users enter the ListenBrainz token "in the app", but where (a
  settings section?) and whether it ships with this change is undecided. Until then
  `lb_token_enc` stays empty.
- **Service account vs users**: the wizard's ND admin credentials also sit in `.env`
  (`NAVIDROME_USERNAME/PASSWORD`) as PV's service login for library work. Fine for one
  user; with several, background jobs should act per user.
- **Per-user ND writes** (smart playlists etc.) without storing plaintext passwords:
  decrypt per call, fresh salt + `t=md5(pw+salt)` per Subsonic request; whether an admin
  service account can assign playlist owners via ND's API is unverified.
- **`needs_reauth`**: when ND rejects a stored password (Subsonic error 40), mark the
  user, pause their jobs, revoke their sessions, show a banner.
- Guardrails from earlier notes: ND DB read-only (`mode=ro`), writes via ND API only,
  auto-backup `navidrome.db` before wizard/bulk operations, dry-run previews.
