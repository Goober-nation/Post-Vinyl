# Troubleshooting

## A song didn't download

| What you see | What happened | What to do |
|---|---|---|
| Listed under **Not found** | No peer had a file that passed the [matcher](search-and-matching.md#the-file-matcher) | Try **Request again** later, or the slskd tab. Peers change through the day. In testing about 1 song in 5 wasn't findable, mostly non-Latin and long-tail ones |
| Failed, "not downloaded" | Peers were found, but none started sending | **Request again** |
| Failed, "bad download" | The last transfer started and broke, or was too slow | **Request again** |
| Nothing appears | The request is waiting or searching, and these aren't listed yet | Check your download source is on, the queue isn't paused, and the slskd light. Searches run one at a time, 4 a minute, across all users |
| Done, but not in Files | Still in the pipeline, or it failed verification | Wait a minute. A file that fails verification is not retried: see below |
| A wrong version arrived | One-letter titles are hard to search and match | Delete it and pick the file in the slskd tab |

Some names return nothing because the Soulseek server filters them; rights-holder phrases such as
some artist names are blocked. Peers never answer those searches. Try the slskd tab with another
spelling, or the album name.

## Files

**Done, but the file never reached the library.** It failed verification: not found, not audio,
wrong length or cut off. The file stays for `unverifiable_keep_hours`, then it's deleted and marked
missing. **Request again** isn't offered for finished downloads in the tab, so request the song
from Search.

**In the library without tags.** Tagging failed twice, 30 seconds apart. Common causes: MusicBrainz
is off for the requester or unreachable, or a hand-picked file with no ListenBrainz token or no
good match. Files shows "Tagging failed", and **Retry tagging** tries again.

**No cover on Ogg or Opus files.** These don't get a cover written, by design.

**Files stuck after download, log says no admin.** No admin has logged in with a working password.
Log in as a Navidrome admin.

## Searching seems stuck

| Sign | Cause | Fix |
|---|---|---|
| Log: "looks throttled" | The Soulseek server is limiting the account. Results go near-empty while slskd looks fine | It pauses for 10 minutes and retries. Restarting slskd doesn't help. Lower `searches_per_minute` if it recurs |
| No searches, warning each minute | slskd isn't logged in or has no distributed-network parent. Without a parent, searches come back near-empty, so Post-Vinyl waits | Wait; after a restart it took about 5 minutes. If it stays, check slskd. Another client on the same Soulseek account kicks it off, and slskd doesn't log back in by itself, so restart it |
| Everything waits, including pulls | The queue is paused | Resume it on the Downloads tab |

## Pulls

**A pull isn't moving.** It starts a song only when no request from any user is waiting or
searching. A paused queue, a user with their download source off, or a long list of manual requests
holds it. It also needs MusicBrainz on and reachable for the pull's owner.

**A song is missing from the playlist.** Only songs in the library join. Failed songs stay out, and
the pull shows the count.

**"Add your ListenBrainz token".** Add it in Settings. If it stopped working, save a new one.

## Login and access

| Message | Cause | Fix |
|---|---|---|
| Keeps returning to login | Plain HTTP on a non-localhost address, so the `Secure` cookie is dropped | Use HTTPS |
| Logged out after 30 days of steady use | The browser cookie's age is set at login and isn't renewed | Log in again |
| `403 cross-site request refused` | `Origin` doesn't match `PV_PUBLIC_ORIGIN`, or `Host` when unset | Set `PV_PUBLIC_ORIGIN` to the address you open |
| Too many failed logins | 20 failures in an hour lock all logins until the window ends, for everyone. Running sessions keep working | Wait, or restart the app, which clears the count |
| Navidrome isn't reachable | Login is checked against Navidrome | Start Navidrome |
| Admin action: "log in again" | Your saved Navidrome password is out of date | Log out and in |
| User removed in Navidrome still logged in | Only noticed when their saved login is tried, up to 30 days | Delete them from the Admin tab instead |

## Changing passwords or wiping Navidrome

A password changed in Navidrome makes the saved login fail. The user is marked as needing to log
in again and their sessions end, background work moves to the next admin, and playlists skip them.
Logging in fixes it.

Wiping Navidrome but keeping Post-Vinyl's database has the same effect for its old users.

## Container

| Sign | Cause |
|---|---|
| Stops at start with a config error | A required value is missing, or the YAML has an unknown key. The message names it |
| `throttle_check_query` "must be a positive str" | Quote the value: `"2020"` |
| Refuses to start after going back a version | The database is newer than the app. Restore a backup |
| Settings save refused: "can't write" | The config folder is mounted read-only |
| `slskd_config/incomplete` grows | Clear it from time to time. It holds songs that failed mid-download |

## Known limits

- Requests that are waiting or searching have no list and can't be dropped from the UI.
- Finished downloads have no **Request again** button.
- In Rename, only title, artist and album affect the lookup. The other fields don't change it.
- The pulls panel stops updating after one failed poll. Reload the tab.
- After being logged out by the server, the header lights may stop updating. Reload the page.
- MusicBrainz search reads one page of 100 hits. A very common title may not show its best match,
  and its exact-phrase search misses spelling variants. Deezer may find these.
- A different remix of a remix can pass the matcher when its length is close.
- Falling back to another download source isn't built, as slskd is the only one.
