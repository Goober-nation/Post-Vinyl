# Search test v2: which query shapes find the song

Research for milestone 2 (#73–#77), run 2026-10-27 on a search-only slskd with its own test
account. Decides the query order for track requests. How queries are named is in
[normalization.md](normalization.md); how returned files are judged is in
[slskd-matching.md](slskd-matching.md).

## Setup

- **Seeds**: 100 songs + 20 traps from the normalization snapshot (#73): Cyrillic 18, other
  scripts 14, multi-artist 14, version 8, accented Latin 16, chart 15, long tail 15. Names are
  ListenBrainz names in the original script, main artist only.
- **Shapes** (#75):
  - `L0`: longest title word + longest artist word (a bracket qualifier like "remix" in between)
  - `L1`: 2nd-longest title word + artist word
  - `T`: full title
  - `TA`: full title + main artist
  - `B`: full album
  - `F:T`: title with accents folded, only where that changes it
- **Plan**: 513 unique queries; 512 searched (one, `take` for a-ha, was skipped).
- **Stop rule**: file count unchanged for 8 s, polling every 2 s, never before 5 s; hard stop at
  30 s or 600 responses.
- **Labelling** (#76): a peer is *helpful* when it has a file with the title in the filename,
  the main artist in the path, no excluded version word, and a length within ±3 s. Files that
  report no length are counted apart as *unverified*, never as helpful. *Found* = at least one
  helpful peer.

Counts only: no peer names or paths (public repo).

## Results per shape

| Shape | Seeds | Found | Helpful peers (median) | Free slot (median) | Peers answering (median) | Peer precision |
|---|---|---|---|---|---|---|
| L0 | 99 | 74 (75%) | 5 | 3 | 11 | 67% |
| L1 | 46 | 36 (78%) | 8 | 6 | 30 | 38% |
| T | 100 | 71 (71%) | 4 | 3 | 48 | 6% |
| TA | 100 | 73 (73%) | 4 | 3 | 5 | 84% |
| B | 97 | 61 (63%) | 2 | 2 | 75 | 3% |
| F:T | 11 | 9 (82%) | 1 | 1 | 2 | 62% |

*Peer precision* = helpful peers / peers that answered. T and B pull in mostly unrelated peers.
L1 and F:T only exist for some seeds, so their rates are on a smaller, different set.

**Traps**: 0 of 81 trap (seed, shape) pairs had a helpful peer. Before the no-length rule, 8
did, all through files with no reported length.

## When one shape fails, which rescues it?

Row: seeds where that shape found nothing. Cell: how many of those the column shape found
(only seeds where both shapes ran).

| Fails ↓ \ rescued by → | Failed | L0 | L1 | T | TA | B | F:T | Any other |
|---|---|---|---|---|---|---|---|---|
| L0 | 25 | – | 0/7 | 3/25 | 2/25 | 4/23 | 2/4 | 5/25 |
| L1 | 10 | 3/10 | – | 2/10 | 3/10 | 1/10 | 1/2 | 3/10 |
| T | 29 | 6/28 | 3/11 | – | 5/29 | 5/27 | 2/4 | 9/29 |
| TA | 27 | 4/27 | 2/9 | 3/27 | – | 3/25 | 2/4 | 7/27 |
| B | 36 | 17/36 | 7/16 | 14/36 | 14/36 | – | 1/3 | 18/36 |
| F:T | 2 | 0/2 | 0/1 | 0/2 | 0/2 | 0/2 | – | 0/2 |

- L0 is the best rescuer when T, TA or B fails.
- When L0 fails, little rescues it (5 of 25 by any other shape).
- B fails most often; nearly everything rescues it.
- F:T rescues 2 of the 4 accented seeds that L0, T and TA missed; one seed was found only by F:T.
- Only 2 seeds were found by exactly one shape (one by L0, one by F:T).

## Ordered strategies (out of 100 seeds)

| Order | Found |
|---|---|
| L0 alone | 74 |
| TA → L0 | 77 |
| L0 → T | 77 (of 99) |
| L0 → B | 78 (of 99) |
| TA → L0 → T | 79 |
| L0 → TA → T | 79 |
| All six shapes | 80 |

**Ceiling: 80 of 100.** The other 20 seeds aren't found by any shape (Cyrillic 7, other scripts
5, version 2, long tail 3, accented 2, multi-artist 1). More queries don't reach them; a
different name might (e.g. a Latin spelling for Cyrillic and Korean artists).

## Per seed bucket

| Bucket | Seeds | TA | TA → L0 | TA → L0 → T | All shapes |
|---|---|---|---|---|---|
| chart | 15 | 14 | 15 | 15 | 15 |
| multi_artist | 14 | 13 | 13 | 13 | 13 |
| accented | 16 | 12 | 12 | 13 | 14 |
| long_tail | 15 | 11 | 12 | 12 | 12 |
| cyrillic | 18 | 10 | 11 | 11 | 11 |
| other_script | 14 | 8 | 9 | 9 | 9 |
| version | 8 | 5 | 5 | 6 | 6 |

## Format of helpful peers' best file

| Shape | Lossless | MP3 320 | MP3 256–319 | MP3 < 256 | Other |
|---|---|---|---|---|---|
| L0 | 54% | 30% | 2% | 9% | 5% |
| L1 | 51% | 32% | 2% | 10% | 5% |
| T | 58% | 25% | 2% | 10% | 5% |
| TA | 56% | 29% | 2% | 8% | 5% |
| B | 62% | 22% | 2% | 8% | 6% |
| F:T | 50% | 33% | 0% | 0% | 17% |

## Decision (user, 2026-10-28, #77)

**Track query order: L0 → TA → T.** The next query runs only when the previous one found no
helpful peer. On this run that finds 79 of 100 seeds (the ceiling is 80), and L0 and TA are
the two most precise shapes, so the first queries return few unrelated peers.

- B (album) and F:T (accent-folded title) are not in the ladder: B is the weakest single shape
  and imprecise; F:T only applies to a few accented titles (it found one seed nothing else did).
- The missing ~20% isn't reached by more query shapes; a different name (e.g. a Latin
  spelling for Cyrillic and Korean artists) is the open lead.

## Running the test: what went wrong and what held

| Event | What happened | Fix |
|---|---|---|
| Lost network parent | Logged in but without a distributed-network parent, searches came back near-empty and looked real (~110 searches) | Runner waits for login **and** a parent before every search and drops a result if either is gone after it; the watchdog restarts slskd after 2 min without a parent |
| Account throttled | Back-to-back searching (~5–6 a minute) was throttled by the Soulseek server after ~240 searches: "44" got 36 responses vs 581 on another account, still 20 min later. An empty slskd database didn't fix it, so it wasn't database bloat | 4 searches a minute + a canary search ("2020", healthy = 600 responses, pass ≥ 300) before the run and every 20 searches; on a failed canary the last 20 results are set aside and redone |
| After the fixes | 17 canaries, 2 failed (one bad patch at the start, 20 results redone); 10 short parent losses, one long enough for a watchdog restart | – |

No documented Soulseek search limit was found online; 4 a minute is the measured safe pace.

## Not verified

- 100 seeds: one seed is one percentage point; small differences are noise.
- One run, one account, one evening; popularity and peers online vary by time of day.
- The labeller's verdicts were hand-checked on samples (all correct), not fingerprinted against
  the audio.
