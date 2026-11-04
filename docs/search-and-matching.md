# Search and matching

How a song becomes Soulseek queries, how returned files are judged, and which peer and file win.
The numbers come from [search test v2](research/search-strategy.md),
[normalization](research/normalization.md) and [how peers name files](research/slskd-matching.md).

**slskd decides.** Every naming choice was settled by which form finds the most helpful peers, not
by which looks cleanest.

## Catalog search

A typed search goes to the user's catalog sources in order, and the first that answers with results
is shown. An error or an empty answer falls through to the next. Browsing stays on that source.

Track results are re-ordered, because MusicBrainz's own order is unstable for tied scores (5 equal
queries gave 3 different top-5s). Best first (`core/catalog_order.py`):

1. Not an excluded version the query didn't ask for, and not a music video.
2. Has an Official release on a plain Album, Single or EP.
3. Higher source score; a source with no scores keeps its own order.
4. Earlier first-release date.
5. Source id, so the order never changes between calls.

Album and artist results keep the source's order. Deezer artists with no albums are dropped.

## Query names

`core/normalize.py` builds the names queries use:

| Rule | Why |
|---|---|
| Drop `(feat. X)`, `[ft. X]`, `(with X)` and a `feat. X` tail | Join words cut results the most |
| Drop credits written into titles (`+ Zara Larsson`, `x stromae`) | Same |
| Main artist only: the first non-featured credit | Dracula: 18 helpful peers with the full credit, 565 with the main artist |
| Drop a leading "The" from the artist | |
| Keep script, accents, case, punctuation and version suffixes | The original wins: Высоцкий 61 peers, Vysotsky 19 |

No transliteration. Folding is the matcher's job, not the query's.

## The query ladder

Track requests run **L0 → TA → T** (`core/strategy.py`):

| Shape | Query | Found alone (of 100) |
|---|---|---|
| `L0` | Longest title word + longest artist word, with a bracket qualifier like "remix" between | 74 |
| `TA` | Full title + main artist | 73 |
| `T` | Full title | 71 |

Together they find 79 of 100, and every shape tested found 80. The missing songs aren't reached by
more shapes; a different name might.

A query **ends the ladder with 5 or more helpful peers**, since one peer may be flaky. Otherwise the
next runs, and helpful peers from all queries are pooled. The song is `found` with at least one.

Word picking stays in each word's own script. A Latin word needs 3 or more letters, any other script
2 or more. Stop words, version qualifiers and digit-only words are dropped.

**Each query stops** when the file count is unchanged for 8 s, polled every 2 s, never before 5 s,
with a 30 s timeout and 600 responses at most. Each is recorded in `slskd_searches` with its shape.

Album queries (`LW`, `BA`) and an album-folder check exist in `core/`, but nothing uses them yet: an
album is requested as one search per track.

## The file matcher

`core/matcher.py`, shared with the benchmark. A **helpful peer** has at least one correct file and
isn't banned; 40 copies on one peer count once.

Both sides are folded first: case, accents on Latin letters, punctuation, `&` as "and", `$` as "s",
dotted acronyms joined. Non-Latin letters stay whole.

A file is **correct** when:

1. It's audio, judged by the name because slskd's `extension` is often empty, and not locked.
2. No excluded version word is in its filename or parent folder, unless the song itself has it.
3. The title's main part is an in-order phrase in the filename. Bracketed title words may be
   anywhere.
4. The main artist is an in-order phrase in the path. Names of 2 characters or fewer must be a whole
   segment, so `1975 09 xx Memphis` isn't The xx.
5. Its length is within ±6 s of the recording the user picked, which covers silence at the tail. A
   file reporting no length is not correct. A song with no length skips this rule.

| Label | Meaning |
|---|---|
| `correct` | Passed |
| `locked`, `not_audio` | Rule 1 |
| `excluded:<word>` | Rule 2 |
| `no_title`, `no_artist` | Rules 3, 4 |
| `no_length`, `duration` | Rule 5 |

The title is matched in the filename because it's there 99.6% of the time. The artist is spread
across filename and folders, so the whole path is searched. The album is in the path only about 60%
of the time, so it isn't required.

### Excluded versions

Cover, live, acoustic, a cappella, mix and remix, extended, solo, reference, home session,
soundtrack and OST, ambient, edit and radio edit, karaoke, nightcore, sped up, slowed, 8D audio,
mashup, bootleg, tribute, "made famous by", "in the style of", instrumental, demo, loop, flip,
backing track, drumless, BBC, Peel and radio sessions, podcast, audiobook, voiceover. Common Russian
forms count too.

- Whole words and phrases only. "Dub", "session", "book", "cast" and "voice" alone are left out as
  too broad.
- **The same song**: remaster, deluxe, expanded, bonus track, single or album version, year
  suffixes, and "original mix".
- **"live" reaches further**: any folder counts, joined forms like `Livebox`, `LiveAt`, `Live1999`
  count, and so does a folder with a full date, which marks a concert recording. Never Liverpool or
  Lively.
- Other words count only in the filename and parent folder, because higher folders were mostly
  categories.

Pulls apply the same list to titles before anything is requested.

## Choosing

**Best file per peer** (`file_key`):

1. Lossless before lossy. Lossless means bit depth and sample rate are reported, slskd's own rule,
   or a lossless extension.
2. Among lossless: CD quality (16/44.1) first, then unknown format, then hi-res by depth × rate.
   Among lossy: highest bitrate; the VBR flag is ignored.
3. The larger file.

**Peer order** (`core/ranking.py`): free upload slot first, then upload speed.

## Limits that protect the account

Searching too often gets the **account** throttled by the Soulseek server: results go near-empty
while slskd looks healthy, and restarting doesn't help. No limit is documented; 4 a minute held and
5–6 was throttled after about 240 searches.

`services/slskd_gate.py` is the only way to search:

- One search at a time, at most `searches_per_minute` started in any 60 s.
- **Ready** means logged in and holding a distributed-network parent. Logged in without a parent
  gives near-empty results that look real (about 110 such searches in one run). Not ready waits with
  no limit.
- Parent lost during a search: the result is dropped and the same query runs again.
- **Empty results need confirming before `notFound`.** They're trusted if any search since returned
  at least `throttle_check_min_responses` (300). Otherwise the check search `"2020"` runs, and a
  failure pauses all searching for 10 minutes.

## Filtered phrases

The Soulseek server sends clients a list of excluded phrases, mostly from rights holders. Compliant
clients never answer a search containing one, so it returns 0 peers. These are exact phrases:
"Kendrick Lamar" is dead while "alright kendrick" works. This is why `L0` splits names well.

## The slskd tab

A typed search runs one query through the same gate, and `found` means any file came back. Files
are labelled only for lock, audio and excluded words, against the query. A pick is checked against
the stored results, becomes its own song named from its path (`core/file_song.py`), and skips
searching.

Repeats are one request per user and query.
