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
  service skips ListenBrainz and nothing else calls it yet. Can't run before milestones 9–10
  (#114).
- [ ] **MusicBrainz paging past 100 hits** isn't built (Hyperballad had 90, Feather 3; Creep
  by Radiohead has 194, #114). Not a live check: a feature to decide on.

## First admin setup (#97)
- [ ] **PowerShell and the macOS terminal**: only run from zsh; the commands are plain
  `docker compose`.
- [ ] **The setup override on the server's own compose file** (only the test compose ran).

## slskd gate (#100)
- [ ] **A real network-parent loss during a search** → result dropped, query redone. Ran
  live only as slskd going away mid-search (#115: rerun once ready); a parent lost while
  slskd stays up can't be forced.
- [ ] **A real throttled account** → the "2020" check fails, searching pauses 10 min, the
  request reruns its ladder. Can't be forced without getting the test account throttled.

## slskd search (#101)
- [ ] **A crashing search** → released to `requested`, retried once, then `notFound`: slskd
  stopping mid-search goes the lost-readiness way instead (#115); a crash while slskd stays
  up couldn't be caused.
- [ ] **slskd refusing a query** (4xx while ready) → treated as a crash: no query slskd
  refuses was found (#115).
- [ ] **A cancelled search that never ends** at slskd → the 30 s deadline: can't be forced.

## slskd download (#102)
- [ ] **A ban expiring**: the shortest ban is 1 day (#115 saw a ban reached live).
- [ ] **A peer going offline mid-transfer** (also open from milestone 3, #79): can't be
  forced; none happened in #114/#115.
- [ ] **slskd resuming a cut-off file by itself** (seen once live in #115, saved outside our
  folder): forgetting that copy is unit-tested only; the next live restart had no resume.
- [ ] **A peer answering the queue-place ask with an error** (from #111): not seen.

## Files (#103)
- [ ] **An old copy outside the library is kept** (the library guard): no app path puts an
  indexed copy outside the library, so it can't be forced without editing our database (#114).
- [ ] **The server's Navidrome** still indexes all of `/music` (downloads included); only
  the test stack was switched to `/music/library`.

## Tagging (#104)
- [ ] **The album-only fallback** when the release's disc/track details can't be had (needs a
  recording with such a release; none came up in #114).
- [ ] **The "earliest official release of any kind" fallback** (no plain album/EP/single).
- [ ] **ListenBrainz lookup rejecting a mismatch** (wrong artist or length): only the
  successful lookup ran live. #114 tried twice: a wrong artist got no ListenBrainz hit at all,
  a wrong title got a hit that fit the file's length (correctly accepted).
- [ ] **Lyrics surviving the rewrite** on real peer files (ReplayGain did on a real M4A, #114;
  no downloaded file carried lyrics).

## Pipeline and upkeep workers (#105)
- [ ] **Navidrome not listing a moved file yet** → linked on a later tick: every move in #114
  (9) was listed on the first scan; can't be forced without a scan that ends early.

## Across the loop
- [ ] **Several users at once** (per-user source preferences, requests and LB tokens ran
  live with one user only).
- [ ] **Long runs**: hours of workers ticking, Navidrome sessions expiring for real (only a
  forced expiry ran live), and slskd keeping searches for days.
