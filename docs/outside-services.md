# Outside services

What each service really does, checked against its source or with live calls at the versions in
[self-hosting](self-hosting.md#upgrades). Read this before touching a client.

## slskd

- **Queueing**: HTTP 200 even when every file failed, 207 when some did; failures are in the body,
  which also carries the transfer ids. A file the peer lacks is accepted (201) and fails later. The
  reason is in `Transfer.exception`, such as "Banned" or "Overwhelmed".
- **Searches**: responses are stored only when a search ends, so stop it before reading. After a
  cancel on 0.26.2 it reads complete at once while its responses stay `[]` for about 0.5 s.
  `searchTimeout` is milliseconds, counted from the last response.
- **Codes**: 429 means another start or queue call is running. 409 on a search start means not
  logged in.
- **Files**: `extension` is mostly empty (512 of 825 live files), and `length` is often missing
  (214 of 1348). FLACs have no `bitRate`. The summary count can differ from the response list, so
  count from the list.
- **Readiness**: from `GET /api/v0/application`: `server.isLoggedIn`,
  `distributedNetwork.hasParent`. After a restart the parent took about 5 minutes. Short losses are
  common (10 in 2 hours).
- **Paths**: the batch's `options.destination` overrides slskd's own subfolder pattern, and the
  transfer API returns no local path, so ours is built from that folder plus slskd's sanitized base
  name.
- **Transfers**: `cancel_download(forget=True)` drops the record and leaves the file.
  `Completed, Aborted` happens when the remote size changes, and counts as failed.
- **Restart**: slskd may re-request a cut-off file itself, saving to `<peer>/<remote folder>/`.
- **Kicks**: another login on the same Soulseek account logs it out, and it doesn't log back in by
  itself.
- API keys default to the read-only role. Post-Vinyl needs one that can search and download.
- `retention.search` unset means searches are kept indefinitely, which re-reading relies on.

## Navidrome

- Subsonic errors arrive as HTTP 200 with `status: failed`.
- Login gives a JWT and a Subsonic token. Logins are limited to 5 per 20 s (429).
- Path lookup through the native API is a prefix or substring match, so the client compares the
  returned path exactly. Subsonic's `path` is built from tags.
- `lastScan` before the first scan is `0001-01-01T00:00:00Z`.
- The same path keeps the same song id on replacement. A move changes it.
- `PUT /api/user/{id}` replaces the whole user: a partial body blanks the username. `POST /api/user`
  accepts an empty `userName`. A taken name is 400 `ra.validation.unique`.
- An admin can delete themselves and leave no admin.
- `createAdmin` works for anyone while there are no users.

## MusicBrainz

- 1 request a second per IP, and every request is refused while over it. The limiter is shared, and
  a 503 backs off.
- **Search ranking isn't stable**: tied hits come back in a different order per call, so `limit=N`
  gives different subsets. Fetch all tied hits (up to 100 a page) and order them yourself.
- Search results have no ISRCs, and many recordings have none at all.
- Searching albums without an artist mostly finds tributes. "Album" release groups include live
  albums and compilations (`secondary_types`).
- Writes "JAY‐Z" with U+2010.

## ListenBrainz

- See [pulls](listenbrainz-pulls.md) for recommendations and radio.
- Name lookup needs a token.
- Can be blocked from some hosts; use the outgoing proxy.

## Deezer

- Errors arrive as HTTP 200 with an error object (800 means no data).
- The advanced-search `artist:"…"` field matches titles, so searches send plain "title artist".
- An ISRC lookup returns one track, though several can share it.
- `artist` is the main artist only; featured artists are in `contributors`.
- Romanizes non-Latin names ("Hikaru Utada" for 宇多田ヒカル), writes "JAŸ-Z", and puts version labels
  in titles. So its names are poor Soulseek queries; matching to MusicBrainz or ListenBrainz gives
  the original script.
- API docs now need a login. `nb_album` identifies empty artist profiles.

## Soulseek

- Searches are broadcast, so every matching peer connects to you. Thousands of connections is normal.
- See [limits](search-and-matching.md#limits-that-protect-the-account) and
  [filtered phrases](search-and-matching.md#filtered-phrases).
- Paths use backslashes.
