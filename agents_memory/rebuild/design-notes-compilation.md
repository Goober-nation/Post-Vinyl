# Design notes: search & matching rebuild (decided changes)

Everything decided for the search pipeline from 2026-10-02 to 2026-10-04, in one place.
Sources: issues #34 (benchmark), #63 (query strategy), #64 (album search),
#65 (filters/matcher plan). Numbers come from the corpus benchmark (`scripts/corpus/`).

**Status as of 2026-10-25: only §1's limit/cap raise is live. Everything else is
decided but not implemented** (dev still has `run_ladder`, first-file-only results and
the 3 s early stop).

---

## 1. Search settings

| Setting | Before | Decided | Status |
|---|---|---|---|
| `search.response_limit` | 60 | **600** | done: default (9d1b601) + server `config.toml` |
| `search.response_cap` | 250 | **600** | done: same |
| `search.wait_seconds` (deadline) | 10 s | **10 s, unchanged** | — |
| Early stop `response_threshold = 10` after `min_wait_seconds = 3` | on | **removed**; replaced by a **stable-count exit** (stop when the file count stops changing; user 2026-10-25, supersedes "cap or deadline only"). Numbers set by `search-test-v2.md` | todo |
| `response_threshold`, `min_wait_seconds`, `pass_ratio_threshold`, `artist_match_min_words` | in config + UI | **delete** from config, config UI, `config.toml.example`; ignore old values with a log line | todo |

Why 600: ~40–70% more helpful peers than 300 on every query shape, same deadline.
Why no early stop: niche or long queries keep collecting until the deadline; a 3 s stop
cuts exactly those off.

## 2. Bugs in `app/services/search.py` (fix first, as their own change)

1. `_to_search_result` builds **one** result per peer from `files[0]`, so every other file
   a peer returns is dropped before matching. The Search tab shows one file per peer for
   the same reason. Fix: one `SearchResult` per file, locked files excluded.
2. Duration is read from `duration`, but slskd's field is **`length`**, so it's always
   `None`. `test_search_service` passes only because its fake data uses `duration`.
3. `_filter_by_artist` checks only the first file, by raw substring. Remove it; the new
   matcher replaces it.
4. `SearchResult` gains queue length, sample rate and bit depth (affects the search route
   response model and the frontend).

## 3. File matcher (new `app/services/file_matcher.py`)

The rules from `scripts/corpus/label.py`, moved into one shared module that both
`track_requester` and the benchmark import. A file is **correct** for a track when:

1. it's an audio file in the peer's **unlocked** files (locked files never count);
2. no **excluded version word** appears in its filename or parent folder, unless the
   requested song itself has that word (as whole words/phrases, never substrings);
3. the title's main part appears as an **in-order phrase in the filename**; words in the
   title's brackets may appear anywhere ("Old Town Road (Remix)" matches
   "Old Town Road (feat. Billy Ray Cyrus) [Remix]");
4. the artist appears as an **in-order phrase in the path**; names of ≤2 characters
   ("xx", "X", "U2") must be their own path segment (a whole folder, or bounded by
   " - " / "_"), so "1975 09 xx Memphis" doesn't count as The xx;
5. the duration is within **±6 s** of the **recording the user picked** (its MusicBrainz
   length), when the peer reports a length. ±6 s covers silence at the tail end (user,
   2026-10-29; was ±3 s). Songs with many recordings (e.g. Высоцкий, 295–356 s) are matched
   against the chosen one only; the catalog service (milestone 6) keeps that recording's length.

Remaster/deluxe/year suffixes count as the **same song**. Non-Latin text is kept as is
(no ASCII folding for matching).

**Excluded versions** (user's list + the old matcher's extras, merged 2026-10-04):
cover, live, acoustic, a cappella/acapella, mix/remix/rmx, extended, solo, reference,
home session(s), soundtrack/OST, ambient, edit/radio edit, karaoke, nightcore, sped up,
slowed, 8d audio, mashup, bootleg, tribute, "made famous by", "in the style of",
instrumental, demo, loop, flip, backing track, vip mix.
Joined "live" forms count too: `Livebox`, `LiveAt`, `Live1999` (but never
Liverpool/Lively/Oliver). **Not** excluded: remaster/remastered.

**Live run on the test slskd (user, 2026-10-28, #90)**: Radiohead live bootlegs passed as
correct ("Live Music\\Radiohead\\1998-04-06 - Pacific Coliseum\\Side A\\02 - Karma Police.flac").
Now "live" counts in **any folder** of the path, and a folder with a **full date**
(YYYY-MM-DD or YYYY.MM.DD) marks a concert recording (unless the song itself is live). Kept as
is: the folder check for "mix" ("Mixed Music 3" category folders stay excluded). Seen,
not yet handled: a different remix of a remix song ("Old Town Road (Maxun Remix)" for "Old Town
Road (Remix)") passes when its length is within the duration tolerance.
**Settled 2026-10-29** (rule 5 above): Владимир Высоцкий – Кони привередливые found no helpful peer. MusicBrainz
writes "Владимир Высоцкий", Deezer "Vladimir Vysotsky"; peers mostly file under "Высоцкий".
L0 picked "владимир" (tie on length), the matcher needs the full name, and MusicBrainz lists
recordings from 295 to 356 s. Test slskd, names only (no duration): "title + album" found 3
peers (Шемякин album) and 0 (Мустафиди); "title Высоцкий" found 15 (13 with the full-name rule);
TA ("title Владимир Высоцкий") found 13 peers, all matching by name: only the length failed.

**Album requests**: a peer qualifies when one folder holds the album: the folder path has
the album and artist words, no excluded word, and ≥80% of the MusicBrainz track count in
audio files.

**Input from the new slskd client (#78, live 2026-10-28)**: bugs 1–2 in §2 are fixed in
`app/clients/slskd.py`, which returns every file per peer (locked ones flagged `locked`, so
rule 1 filters on the flag) and its `length` (`None` when missing). In one live search, 214
of 1348 files had no `length`, so rule 5's "when the peer reports a length" is common.
**757 of 1348 files had an empty `extension`**, so "is it audio" (rule 1) must read the
extension from the filename, not the field. slskd's search summary count can differ from
the response list (441 vs 435); count from the list.

**Success unit = helpful peer**: a peer with ≥1 correct file, counted once (40 copies on
one peer are worth one). Replaces the old pass ratio (viable files / all files ≥ 0.6).

## 4. Track search strategy (replaces the up-to-5-rung ladder)

**Decided (user, 2026-10-28): L0 → TA → T** for every track request, from the search test
v2 rerun (`docs/research/search-strategy.md`): L0 = longest title word + longest artist word
(bracket qualifier in between), TA = full title + main artist, T = full title. The next query
runs only when the previous one found no helpful peer. Found 79 of 100 seeds (ceiling 80).
B, F:T, AT and LW are not in the ladder. **This replaces the provisional order below**, which
is kept for history (from the first, flawed benchmark).

**Provisional (user, 2026-10-25): the benchmark was flawed.** The rules get re-established and the
test rerun before the final query order is assembled. The L0 → T / AT order below is the
direction, not the final ladder.


Shapes: **L0** = current first rung (longest title word + longest artist word, plus a
bracket qualifier word if present), **T** = full title as given, **AT** = longest album
word + longest title word (no artist).

1. **Usable album known** → search **L0**. If ≥1 helpful peer: done.
2. Otherwise → search **T**.
3. **No usable album** (singles, manual requests, unusable album per §5) → **T first**,
   then L0.
4. **L0 returns zero peers at all** → the artist name is on Soulseek's excluded-phrase list
   (§8) → search **AT** instead (or T when there's no usable album), then T.

At most 2 queries (3 for an excluded artist). The second query only runs when the first
finds no helpful peer.

| Benchmark (600 cap) | Found | Median helpful peers | Avg queries/song |
|---|---|---|---|
| Album tracks: L0, then T | 99% | 180 | 1.09 |
| Album tracks: L0 alone (≈ old first try) | 91% | 150 | 1.00 |
| Singles: T, then L0 | 96% | 34 | 1.04 |
| Singles: L0, then T | 96% | 28 | 1.20 |

LW (album word + artist word) was tested for tracks and rejected: weakest track shape
(other albums by the same artist fill the cap). LW stays the choice for **album** requests.

## 5. Unusable album → skip album-based shapes

Don't use AT/LW (treat as "no usable album") when:
- the album's longest word is also an artist word (self-titled: Weezer – *Weezer*,
  Massive Attack – *Massive Attack*);
- the album name is generic (*Greatest Hits*, *Live*, *Best Of*, *Singles*,
  *Collection*, *Unplugged*, …);
- the album name is only digits or one character (*7*, *21*); slskd rejects queries with
  no word longer than 1 character.

For a single, the title stands in for the album.

## 6. Picking the peer

Among peers with a correct file: **free slot first, then upload speed**. Quality
(lossless > bitrate > size) is recorded but not used for picking yet. Kept from today:
skip blocked peers and peers over the bad-peer threshold; `max_retries_per_track` attempts.

## 7. Album downloads

Stay **one search per track** for now (each track runs the §4 strategy). One LW album
search + folder matching is the later #64 work.

## 8. Soulseek excluded phrases + low-result log

The Soulseek server sends clients a list of excluded search phrases (mostly
rights-holder requests); compliant clients never answer a search containing one, so it
returns **0 peers**. Exact phrases only: "Kendrick Lamar" is dead, "alright kendrick"
works. Seen so far: Gorillaz, Beyoncé/Beyonce, MGMT, Adele, Clipse, "Kendrick Lamar",
"Kanye West", "Purple Rain", "The Beatles", "Frank Ocean",
"Selected Ambient Works Volume II".

**Low-result log** (user, 2026-10-04): whenever a search returns **fewer than 10
responses**, append one line with the query to `/app/data/low_result_queries.txt`
(`app_data/` on the host).

## 9. Search tab (manual search)

All files, grouped by peer. Files that pass the matcher are marked; excluded versions
dimmed.

## 10. Order of work (#65)

1. `search.py` fixes (§2) + no early stop + low-result log.
2. `file_matcher.py` (§3); `scripts/corpus/label.py` imports it; tests carried over.
3. `query_builder`: T and AT builders, unusable-album check (§5).
4. `track_requester`: §4 strategy, helpful-peer success, §6 picking.
5. `mb_resolver`: pass album + MusicBrainz duration.
6. Config cleanup (§1) + Search tab UI (§9).
7. Re-run the benchmark through the app module to confirm the numbers.

---

## Benchmark & corpus (how the numbers were made)

- **Seeds**: `tests/fixtures/corpus/seeds.toml`: 114 seeds (tracks, albums, artists,
  8 negative traps), MusicBrainz-resolved; `singles.toml`: 25 singles on no studio album.
- **Harvest**: separate search-only slskd (own Soulseek test account) + harvester in
  `~/projects/postvinyl-corpus/` on the server; `run_suite.sh <plan> <out> <cap>` runs
  detached, stops the main slskd and always restarts it afterwards; `watchdog.sh`
  restarts a frozen slskd. Every query is recorded raw (no matching, no early stop).
- **Data** (server): `data/corpus` (300 cap, 812 queries), `data/corpus600` (529),
  `data/corpus_singles` (124). Not yet published as a separate dataset.
- **Scoring**: `python -m scripts.corpus.label --corpus <dir> [--plan <plan.json>]`.
- Gotchas: the main slskd and the corpus instance share port 50300, so never run both;
  multi-line YAML `command:` silently dropped harvester flags (fixed, one line now).
