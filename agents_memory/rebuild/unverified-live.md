# Unit-tested only: not yet run against a live instance

Collected 2026-10-29 at the end of milestone 6 (core loop, #95–#105). Everything here has
unit tests (fakes, fake clocks, real SQLite, real audio files) but no run against the real
service. Milestone 7 (wiring + validation) should take each one live, or say why it can't.
When an item gets a live run, move it to the issue's closing comment and delete it here.

Test stack used for the live runs that *did* happen: slskd 0.26.1 → 0.26.2 (through its
configured Soulseek proxy), Navidrome 0.64.2 with `ND_MUSICFOLDER=/music/library`, real
MusicBrainz / ListenBrainz / Deezer / Cover Art Archive. Versions: `work-plan.md`.

## Catalog and requests (#95–#98)
- [ ] **ListenBrainz as a catalog source** for request sources (name → MBID fallback for
  Yandex/Postradio): only the tagging service's lookup ran live (#104); the catalog
  service skips ListenBrainz and nothing else calls it yet.
- [ ] **Concurrent `resolve` / request state changes** (`BEGIN IMMEDIATE`, guarded
  writes): the lock races were tested with two connections in one process, never under
  real concurrent app traffic.
- [ ] **Album and artist search results** keep the source's own order (only track results
  are reordered); the album/artist paths ran live only as part of #95's single check.
- [ ] **MusicBrainz paging past 100 hits** isn't built (Hyperballad had 90, Feather 3).

## First admin setup (#97)
- [ ] **Hidden password prompt on a real terminal**: the live run piped stdin, so `getpass`
  fell back to echo.
- [ ] **The 60 s wait for a starting Navidrome**: Navidrome may already have been up at
  the first call.
- [ ] **PowerShell and the macOS terminal**: only run from zsh; the commands are plain
  `docker compose`.
- [ ] **The setup override on the server's own compose file** (only the test compose ran).

## slskd gate (#100)
- [ ] **A real network-parent loss during a search** → result dropped, query redone.
- [ ] **A real throttled account** → the "2020" check fails, searching pauses 10 min, the
  request reruns its ladder.
- [ ] **The gate's unbounded readiness wait** while slskd stays logged out or without a
  parent for minutes.

## slskd search (#101)
- [ ] **The TA and T rungs and pooling below 5 helpful peers**: every live song ended on
  L0 with hundreds of helpful peers.
- [ ] **A real `notFound`** with the throttle check confirming the empty results.
- [ ] **A crashing search** → released to `requested`, retried once, then `notFound`.
- [ ] **`dispatch.resume()` after a real app restart** (requests left `searching`).
- [ ] **slskd refusing a query** (4xx while ready) → treated as a crash, not a lost parent.
- [ ] **A cancelled search that never ends** at slskd → the 30 s deadline.

## slskd download (#102)
- [ ] **`tooSlow`**: no live transfer was slow enough.
- [ ] **`badDownload`**: no live download ended with a broken last attempt.
- [ ] **A peer reaching the ban** (3 failures → 7 days) and the ban expiring.
- [ ] **Dropping a request mid-transfer** → transfer cancelled, row `cancelled`, peer not
  blamed.
- [ ] **A peer going offline mid-transfer** (also open from milestone 3, #79).
- [ ] **The new 0.26.2 state `Completed, Aborted`**: seen live, but its handling (as a
  failure) is only unit-tested.

## Files (#103)
- [ ] **`missing` detection**: a file deleted outside Post-Vinyl, before the move or after
  indexing.
- [ ] **Deleting a replaced song's old copy** ran live only with both copies in the same
  folder (the same path, so Navidrome kept the song id); a replacement whose new copy lands
  at a *different* path (other album or tags) is unverified.
- [ ] **An old copy outside the library is kept** (the library guard).
- [ ] **The server's Navidrome** still indexes all of `/music` (downloads included); only
  the test stack was switched to `/music/library`.

## Tagging (#104)
- [ ] **Writing M4A/MP4 and Ogg Vorbis/Opus tags**: no such downloads yet (FLAC and MP3 ran
  live).
- [ ] **The album-only fallback** when the release's disc/track details can't be had.
- [ ] **The "earliest official release of any kind" fallback** (no plain album/EP/single).
- [ ] **ListenBrainz lookup rejecting a mismatch** (wrong artist or length): only the
  successful lookup ran live.
- [ ] **ReplayGain and lyrics surviving the rewrite** on real peer files.

## Pipeline and upkeep workers (#105)
- [ ] **Cleanup forgetting transfers at slskd** and deleting failed attempts' folders.
- [ ] **The hourly missing check** across all file states.
- [ ] **Deleting unverifiable downloads after 24 h.**
- [ ] **The one-hour retag and moving untagged after a second failure.**
- [ ] **Re-dispatching requests that waited for a download source** (one turned back on).
- [ ] **Navidrome not listing a moved file yet** → linked on a later tick.
- [ ] **`CoreLoopWorkers` inside the running app** (the app wiring is milestone 7; the live
  run started the workers from a test).

## Across the loop
- [ ] **Several users at once** (per-user source preferences, requests and LB tokens ran
  live with one user only).
- [ ] **Long runs**: hours of workers ticking, Navidrome sessions expiring for real (only a
  forced expiry ran live), and slskd keeping searches for days.
