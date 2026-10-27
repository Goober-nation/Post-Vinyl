# Search test v2: rules (decided 2026-10-25, not built)

Rerun of the slskd search benchmark (#34) after the first run was judged flawed. The final query
order (§4 of `design-notes-compilation.md`) is assembled from this test's results, not the first.

## Decided (user, 2026-10-25)

- **New slskd test runs**; nothing from the first harvest is reused as results.
- **Query construction**: take the strategies from the first test (`scripts/corpus/plan.py`:
  L rungs, W2, T, A, B, D, F, LW, AT), review them, and use them.
- **No pass/fail booleans.** Each query records how many responses it got (and the other counts
  below), and keeps its **slskd search id** so the full search can be pulled from slskd later for
  re-analysis. slskd keeps searches indefinitely (`retention.search` unset), so the corpus slskd
  instance's data must be kept.
- **Seed names: only official, cleaned names from Deezer and MusicBrainz/ListenBrainz.** No
  hand-typed titles.
- **One normalization for every source**: a way to turn any source's track, artist and album
  names into the same `[track, artist, album]` arrays, so the same song from Deezer, MusicBrainz,
  ListenBrainz or a file path always comes out as the same set.
- **Don't store every response.** Log the counts the results need now, plus every count that shows
  up during a run that could be useful later (e.g. the poll trace).
- **Downloadability matters**: free slot, queue length and upload speed are recorded, not only
  "a correct file exists" (how they score is decided after the run, see below).
- **Search stop = stable-count exit** (supersedes the earlier "cap or 10 s deadline only"): stop
  when the file count hasn't changed for a stable window (backlog2 phase 3 proposes 8 s, polling
  every 2 s, minimum 5 s, plus a hard timeout). The test uses the same rule as production.

## Decided (user, 2026-10-26)

- **Stable-count exit**: stop when the file count is unchanged for **8 s**, polling every **2 s**,
  never before **5 s**; hard timeout **30 s**; response cap **600**. Same numbers in production.
- **Normalization is researched and tested first**, as a separate test before the main run; the
  "which source's name is official" question is answered by that research.
- **Normalization strips**: feat./ft. credits; version suffixes (Remastered, Deluxe, years,
  "Album Version"); brackets and punctuation (keep the words inside); accents and case (fold to
  plain lowercase for Latin; keep non-Latin letters).
- **Downloadability**: logged only (free slot, queue length, upload speed per helpful peer);
  scoring is decided after the run.
- **Logged per query** (besides response count, file count, slskd search id): the **poll trace**
  (elapsed, responses, files at every poll); **helpful peers** and **helpful peers with a free
  slot**; **format mix** of correct files (flac/mp3/other, bitrate bands).
- **Seeds: a new batch of ~100 songs, none from the user's library**, picked by Claude: varied
  names, Russian and English with a few other languages, varied popularity, plus traps.

## Normalization test (planned 2026-10-26, runs before the main test)

Goal: the same song from any source normalizes to identical `[track, artist, album]` arrays.

- **Songs: a separate, larger set**, not the ~100 main-test seeds.
- **Stage 1, API sources**: fetch raw names per song from Deezer (`title`, `title_short`,
  `title_version`, `artist.name`, contributors, `album.title`), MusicBrainz (title, artist credit
  with join phrases, release title, disambiguation) and ListenBrainz (metadata lookup). Catalogue
  the real differences first (feat. placement, version suffixes, apostrophes, "&"/"and", "The",
  case, accents), then test the normalizer: per song and per source pair, field-by-field equality,
  per-rule on/off contribution, and a list of every mismatch for review. Offline after fetching.
- **Stage 2, slskd file paths**: from the old corpus (`data/corpus*`), as a **separate stage whose
  data is never combined with stage 1**. Paths are used only to test normalization, not as
  search results.
- **No transliteration**: Cyrillic and Latin spellings stay different (Кино ≠ Kino).
- **Code: a test script first** (`scripts/corpus/`); it moves into the app after it's settled.
- The research answers which source's name is the official one.
- **Song set: ~300 songs** from Deezer charts (several countries, incl. Russia) plus a random
  MusicBrainz recording sample for the long tail.
- **Same song across sources = same ISRC** (Deezer and MusicBrainz carry ISRCs; ListenBrainz via
  the MusicBrainz recording id). Songs without a shared ISRC are dropped.
- **No pass target**: the report gives equality rates per field and per rule, plus the mismatch
  list; the user judges.

## Stage 1 decisions (user, 2026-10-27, #71)

- **Rules dropped**: `version`, `amp`, `punct`, `case`. **Kept**: `feat`, `the`.
  `accents` (strip diacritics from Latin letters, é → e): **queries keep the original
  spelling; the matcher folds accents on both sides** when comparing files (user, 2026-10-27).
  slskd check: peers match accents loosely (a "Sigur Ros" query returned 9,574 "Rós" files), and
  where counts differ the original wins (Café Tacvba 127 vs Cafe Tacvba 79 helpful peers;
  Motörhead and Sigur Rós pairs hit the response/file caps, so no difference shows). "Beyoncé"
  and "Beyonce halo" both got 0 responses (likely a server-filtered word).
- **Sources**: Deezer (behind a client interface) is the main catalogue search the user picks
  songs from. Every song on the final list is then matched to MusicBrainz/ListenBrainz (ISRC
  first, every other way after) to get the original-script names used for slskd queries.
- **Artist**: the query uses the **main artist only**; the full credit goes to the matcher
  (user, 2026-10-27, after this check; replaces "full credit for now"). slskd check 2026-10-27 (title + artist queries, helpful peers): main artist only wins every
  time — Jaded: feat. credit 4 / all names 11 / main 24; Dracula: "with JENNIE" 18 / names 136 /
  main 565; Flores de abraços: 1 / 1 / 6; Dead on the Dance Floor: 2 / 2 / 2. Join words
  ("feat.", "with", "&") in a query cut results hardest.
- **Where each rule lives**: `normalize.py` only builds query names (feat. and title-credit
  stripping, the "The" rule, main artist; everything else as written). Folding accents, case
  and punctuation happens in the **matcher**, on both sides, only to decide whether a returned
  file is the song: Soulseek matches loosely, so returned files are spelled differently from
  the query.
- **Album**: the main album (ListenBrainz's release), not Deezer's single.
- **Credits written into titles** ("+ Zara Larsson", "x stromae"): stripped, not kept.

## Search test v2 query plan (user, 2026-10-27, #75)

- **Shapes per seed**: `L0` and `L1` (first two ladder rungs: longest title word × longest
  artist word, then 2nd title word; L0 carries a bracket qualifier like "remix" when the title
  has one), `T` (full title), `TA` (full title + main artist, new), `B` (full album; skewed for
  tracks but useful data). Dropped: L2–L4, A, W2, AT, D, LW.
- **Variant**: `F:T` only, the accent-folded/symbol-mapped title where it differs from `T`, to
  see whether accents need more investigation. No Deezer-name variant: the MusicBrainz name
  already did better on slskd.
- **Word picking is Unicode-aware**: the old `select_words` folded to ASCII and split on
  anything outside `[a-z0-9']`, so Cyrillic, Hangul and kana titles produced no words. Words
  now keep their script and accents; 2-letter words count in non-Latin scripts.

## slskd decides (user, 2026-10-27)

- **The normalizer's choices are judged by slskd**: which name finds the most helpful peers, not
  which catalogue looks cleanest. The API-equality report (#70) shows where sources differ;
  slskd results settle which form wins.
- **Non-Latin names stay in their original script** (Russian confirmed; Korean too, see below).
  Spot check on the main slskd, 2026-10-27 (helpful peers = peers with ≥1 audio file of that
  artist, and that title where one was searched):

  | original script | peers | romanized | peers |
  |---|---|---|---|
  | стрыкало | 45 | strykalo | 12 |
  | Высоцкий | 61 | Vysotsky | 19 |
  | Би-2 | 74 | B-2 | noise (5% of files on target) |
  | с тобой би-2 | 21 | with you b-2 | 0 |
  | 이은미 | 10 | Lee Eun Mi | 3 |
  | 악동뮤지션 | 12 | AKMU | 62 |

  AKMU is the exception: the group's own Latin name beats the Hangul one, so where a source has
  both, both forms are worth a query step (measure in search test v2).
- Short artist names win when unambiguous ("Spagna" 217 peers vs "Ivana Spagna" 76); a version
  label in the query can zero a search ("vai dar pt ao vivo" 0 vs "vai dar pt" 7 peers).
- Some words are filtered by the Soulseek server: "octopus garden beatles" returned 0
  responses with or without the apostrophe.

## Normalization stage 2 plan (user, 2026-10-27; rescoped the same day, #72)

Goal (rescoped): measure how peers lay out and spell file paths, so the matcher (milestone 5)
has data-backed rules. `normalize.py` only builds query names, so paths are no longer
normalized "into the same arrays".

- **Input**: raw responses from the first harvest (#34), copied read-only from the server into a
  local gitignored cache (ask before touching the server). Ground truth: the old track seeds
  with an MBID, **excluding the `library` bucket**; their names are re-fetched from MusicBrainz
  by MBID, not read from `seeds.toml`.
- **Which files count**: candidates by ±3 s of the MusicBrainz length plus loose word overlap;
  the user hand-checks a random ~100 once to measure the rule's error rate (the rule is partly
  circular with the matcher, so the sample is what keeps it honest).
- **Path → fields**: filename → track, parent folder → album, the folder above → artist. Clean
  track numbers (`01 -`, `1-01`), "Artist - Title" filenames, `_`/`.` as spaces, disc folders
  (`CD3`), backslashes, `(1997)`, `[FLAC]`, extensions; then the stage 1 normalizer.
- **Report** (offline): equality per field and per rule; containment (every seed word somewhere
  in the path) separately from field equality; failure categories with counts; mismatch list.
- **Kept separate** from stage 1 data. Only the script and an aggregate report are committed:
  peer names and full paths stay local (public repo).

## Still to define

- Exact seed list (draft for the user to edit) and the trap count.
