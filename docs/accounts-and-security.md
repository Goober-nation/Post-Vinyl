# Accounts and security

## Accounts

Every Post-Vinyl user is a Navidrome user. There is no public sign-up. Navidrome owns accounts, and
the row here holds a user's history, settings and encrypted secrets. A user's row is created on
first login, or when an admin adds them. `is_admin` is copied from Navidrome at each login.

**First admin.** While Navidrome has no users, the login page offers **Create admin**. It reads
`firstTime` from the config embedded in Navidrome's `/app/` page, the only place that says so, and
calls `createAdmin`. Navidrome answers 403 once a user exists (our `409`). If `/app/` changes, the
form stops appearing and login still works. These routes are open by necessity.

**Admin actions** run in Navidrome first, as the acting admin, then update our row. Each logs in
first, so Navidrome's limit of 5 logins per 20 s applies, and a burst gets a `503`.

| Action | Notes |
|---|---|
| Create | Empty names refused. Taken name `409` |
| Password | The whole Navidrome user is fetched and sent back, because a partial update blanks the name. Your own needs the current password. Ends that user's Navidrome tokens |
| Delete | Refused for yourself, since Navidrome would allow leaving no admin. Pulls cancelled, requests not yet downloading dropped, started downloads finish, the music stays |

A deleted user's row stays hidden as `deleted-<id>` with no secrets, because requests point at it.
Sessions and settings go.

**Changes made in Navidrome.** Users are matched by Navidrome's id (`nd_id`), so a rename keeps the
row, and the next login updates the name. A stale row holding a wanted name becomes `name~id`.

A refused saved login sets `needs_reauth` and ends that user's sessions. Playlist updates skip them,
background work moves to the next working admin, and a login clears it. This is only noticed when a
saved login is tried; there is no periodic check.

## Login and sessions

| Rule | Value |
|---|---|
| Checked against | Navidrome, on every login, so login fails while it's down |
| Session | Random token in cookie `pv_session`; only its SHA-256 is stored |
| Cookie | HttpOnly, Secure, SameSite=Lax, Path=/, 30 days |
| Expiry | 30 days sliding on the server, pushed forward at most hourly. The browser cookie's age is set at login |
| Failed logins | 20 in an hour lock all logins until that window ends. In memory, fixed window. A success doesn't reset it, and Navidrome being down doesn't count |
| Same error | Wrong password and unknown user look alike |

The lock is server-wide and only blocks new logins; running sessions keep working. The accepted
downside is that anyone can lock everyone out for up to an hour.

## Request guards

**Default deny.** Everything answers `401` without a valid session, including unknown paths, so
denial comes before `404`. Open: login, first admin, `GET /api/system/ping`, and `GET` of `/` and
`/assets/*`, which hold no data. `/docs` and `/openapi.json` open only with `PV_DEV`.

**CSRF.** POST, PUT, PATCH and DELETE need `Origin`, or else `Referer`'s origin, equal to
`PV_PUBLIC_ORIGIN`, or to the request's `Host` when that's unset. Neither header gets `403`. Login
is checked too.

**Authorization.** Admin routes re-check the flag. Someone else's request, search or download
answers `404`. The frontend shows admin parts to everyone and refuses them; the API's `403` is the
real guard.

**No paths.** No answer contains our own file paths. Peer paths appear only in the slskd and
Downloads tabs.

## Secrets

| Secret | Stored |
|---|---|
| Navidrome password | Fernet-encrypted in `users`, re-encrypted at each login. Needed so background work can renew the 48 h token with nobody present |
| ListenBrainz token | Fernet-encrypted in `users`. Checked before storing. Readable by no API |
| slskd API key | Environment only |
| Encryption key | `PV_SECRET_KEY`, or `secret.key` beside the database |

A missing or invalid key refuses to start. A wrong key or altered value raises an error, never a
silent empty value. See [backups](self-hosting.md#backups) and
[rotation](self-hosting.md#rotating-the-secret-key).

## Reporting a problem

The repository is public, so please don't put an exploit in a public issue. Use the contact address
in the [project README](../README.md#acknowledgments).
