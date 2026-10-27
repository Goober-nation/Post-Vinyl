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
