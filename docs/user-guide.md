# User guide

You log in with your Navidrome account. Each tab keeps its place when you reload the page, and each
has a Tips panel on the left that you can fold away.

The header shows lights for Navidrome and slskd, checked every 30 seconds. Open them to see every
service and ping one or all of them. "Turned off" means that source is off for you, so it isn't
called. An slskd light that's on means slskd answers, not that it's logged in to Soulseek.

## Search

One box searches songs, albums and artists together. An optional artist field narrows songs and
albums. Each section loads on its own, so one failing doesn't hide the others.

Results come from the first catalog in your order that has any, normally MusicBrainz, then Deezer.
Open an artist to see albums, top tracks and similar artists. Open an album to see its tracks.

**The cart.** Add songs, then request them together, up to 100. The cart lives in your browser.
Songs already in your library are marked and skipped unless you turn on "Re-download songs I
already have". Anything that can't be requested stays in the cart with the reason.

After that Post-Vinyl works alone: it searches Soulseek, picks a peer, downloads, checks, tags and
files the song. Live versions, covers, remixes and similar are skipped unless the song you picked
is one. See [excluded versions](search-and-matching.md#excluded-versions).

## slskd

Search Soulseek directly and pick exact files, for when you want control. Results are grouped by
peer, and locked files, non-audio files and excluded versions can't be picked.

Repeating a search shows its stored results. Only a search that ended with nothing, or that you
dropped, searches again.

Hand-picked files differ from catalog requests:

- Each file becomes its own request, under the name guessed from its path.
- It waits in that peer's queue for as long as it takes.
- If it fails there is no next peer. Pick another file.
- Tagging needs a ListenBrainz token and a good match. Otherwise the file goes in untagged, under
  `<Artist>/<Title>`.

## ListenBrainz

Needs your token, set in Settings. See [how pulls work](listenbrainz-pulls.md).

**Recommendations.** Press **Check for new**. Each list ListenBrainz generated is kept, by date.
Pick a list, set a rank range, preview it, name the playlist if you like, and pull.

**Radio.** Enter an artist and a level (easy, medium or hard). **Reroll** gets a different list,
and nothing is kept until you pull.

Pulled songs download one at a time, behind everything anyone requests by hand, and join a
Navidrome playlist as each one reaches the library. Songs you already have go straight in. A big
pull takes hours. You can run one pull at a time.

**Cancel** removes queued songs. Downloads that already started finish and still join the playlist.

## Downloads

The list shows each download, its peer and its live progress. A bar at the bottom follows active
downloads from any tab, and a toast names each of your downloads as it starts.

Select a row for its attempts.

| Action | What it does |
|---|---|
| **Next peer** | Skips the current peer without counting it against them, and tries the next |
| **Drop** | Cancels a running download |
| **Request again** | Starts over with a fresh search, for failed or cancelled downloads |

**Not found** below the list shows your requests no peer could provide. **Request again** works
there too.

Admins also get **Pause queue** and an **Everyone's** toggle. Pausing stops new searches and
downloads for everyone, and pulls wait too. Running downloads carry on. After **Resume**, waiting
requests start within a minute.

Requests still waiting or searching aren't listed here yet, so they can't be dropped from this tab.

## Files

Everything Navidrome has, whether Post-Vinyl downloaded it or not. A new download appears after a
short while, once it has been tagged and scanned. Any user can browse. Rename, retag and delete are
for admins, because they change the file for everyone.

- **Rename**: edit the names, and Post-Vinyl looks up a match and suggests the full tag set.
  Accept it to rewrite the tags and move the file. If nothing matches you can strip the tags
  instead, and the file moves to `<Artist>/<Title>`. Only title, artist and album affect the lookup.
- **Retry tagging**: shown when tagging failed. It tries again from the file's name and tags.
- **Delete**: removes the file for every user. Requesting the song later downloads it again.

## Settings

**Personal** applies at once and only to you:

- catalog and download source order, and switching sources off
- results per search and rows per page
- which downloads the tab shows first
- your ListenBrainz token, which is checked before it's saved and never shown again

**Server-wide** is for admins and applies after a restart. See
[configuration](configuration.md).

Turning your download source off makes your requests wait, not fail. Turning MusicBrainz off means
your downloads aren't tagged.

## Admin

**Users**: add users, change passwords and delete users. Accounts live in Navidrome, and a change
here happens there first. Deleting a user cancels their pulls and requests not yet downloading,
while their music stays. You can't delete yourself.

**Peers**: peers that keep failing are banned for a while. You can clear one or all of them.
