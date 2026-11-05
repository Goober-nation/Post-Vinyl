# ListenBrainz pulls

A **pull** is a named batch of songs chosen from ListenBrainz. It downloads in the background and
fills one Navidrome playlist as songs arrive. Users pick songs directly, so pulls skip any approval
step. They need a stored token and `listenbrainz` on for the user, and each user runs one pull at a
time.

## Choosing songs

| | Recommendations | Radio |
|---|---|---|
| Endpoint | `GET /1/cf/recommendation/user/{name}/recording?count=1000` | `GET /1/explore/lb-radio?prompt=artist:(NAME)&mode=easy\|medium\|hard` |
| Auth | None, but needs the username, stored when the token is checked | User token |
| Returns | Up to 1000 rows with MBID and score, no names | JSPF with names (33 tracks in the one run measured) |
| Names | Filled by one metadata lookup | Included |
| Stored | Every generation, per user, in `lb_playlists` | Only the chosen songs, at pull time |

A recommendation list is identified by ListenBrainz's `last_updated`. **Check for new** stores a
generation it hasn't seen. `204` means none has been generated yet. How often lists refresh isn't
documented; a community post says weekly, and samples were 13 days and over two months old. The
endpoint is marked experimental.

Excluded versions and songs with no title are filtered out before preview, using the matcher's list.
Radio returns those often, so the filter is required.

Default names: `recommendations-<date>`, `radio-<artist>-<date>`.

## The queue

Search is capped at 4 a minute, and resolving a new song costs MusicBrainz calls at 1 a second, so
1000 songs take over 4 hours. Creating 1000 requests up front would put them all ahead of later user
requests. So a pull's songs wait in `pull_items`, and the `pull-feeder` worker releases them.

Every 5 s it:

1. Marks settled pulls `done`: nothing queued, and every requested item ended.
2. Stops if **any** request with a song is waiting or searching, from any user. A request in
   `midDownload` doesn't block, so a slow peer queue can't stall a pull.
3. Takes the next queued item of the oldest running pull:

| Item | Result |
|---|---|
| Not in the catalog | `failed`, then the next item |
| Song already has an `indexed` file | `reused`, straight to the playlist, then the next item |
| Navidrome has a song of that title and artist, however it got there | `reused`, with `library_id`, straight to the playlist, then the next item |
| Navidrome can't be checked | Stays `queued`, so nothing downloads blind. Stops until the next tick |
| Otherwise | Song resolved, request created and dispatched: `requested`. Stops until the next tick |

So one song at a time goes ahead of a download, and **user requests always go first** without
dispatch knowing about pulls. A search already running finishes first. Resolving uses MusicBrainz,
so it needs that source on for the owner.

Progress needs no new state: `pull_items.request_id` leads to the request's `track_state`.

## Playlists

The `pull-playlists` worker keeps each pull's playlist current. `services/playlists.py` takes the
items whose song has an `indexed` file or a `library_id`, in position order, and fully rewrites the
Navidrome playlist.
It's the only writer of Navidrome playlists. Failed songs stay out, and a playlist removed in
Navidrome is recreated.

Playlists are written with the user's own Navidrome session, so they belong to that user. Users
marked `needs_reauth` are skipped until they log in.

## Cancel

The pull becomes `cancelled`, queued items are cancelled, and its requests not yet downloading are
dropped. Started downloads finish and still join the playlist.

Deleting a user cancels their pulls the same way, and drops all their requests not yet downloading.

## Tables

- `lb_playlists`: every song of every generation.
- `user_lb_playlists`, `user_radio_requests`: one table per way of pulling. Each `id` reuses an
  `origins` id, so requests can point at it with a real foreign key.
- `pull_items`: the chosen songs and the queue.

"One running pull per user" is checked in the service, because no single index covers both tables.

**Adding a way to pull**: one detail table, one `origins` kind and one source of items. `pull_items`,
the feeder, cancel and playlists stay as they are.

See also [the data model](data-model.md#pulls).
