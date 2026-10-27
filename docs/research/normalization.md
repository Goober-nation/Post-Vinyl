# Name normalization for slskd queries

Research for milestone 1, stage 1 (#69, #70, #71), 2026-10-27. Turns catalogue names into
the names an slskd query is built from. **slskd results decide every choice**: which form
finds the most helpful peers, not which catalogue looks cleanest.

How files returned by slskd are compared to the song is the matcher's job: see
[slskd-matching.md](slskd-matching.md).

## Decisions

| Topic | Decision |
|---|---|
| Catalogue | Deezer (behind a client interface) is the search users pick songs from. |
| Names for slskd | Each picked song is matched to MusicBrainz/ListenBrainz (ISRC first, other ways after); their names build the query. |
| Script and spelling | Kept as the source writes them: original script (Высоцкий, 이은미), accents (Café), case, punctuation, "&", version suffixes. No transliteration. |
| Artist in the query | Main artist only (first credit that isn't featured). The full credit goes to the matcher. |
| Album | ListenBrainz's main album, not Deezer's single. |
| Removed from titles | "(feat. X)", "[ft. X]", "(with X)", a "feat. X" tail, and credited artists written into the title ("+ Zara Larsson", "x stromae"). |
| Removed from the artist | A leading "The". |
| Folding case, accents, punctuation | Not in queries. The matcher folds them on both sides when checking returned files. |

Code: `scripts/corpus/normalize.py` (moves to `app/core/normalize.py` once settled).

## Song set

292 songs fetched from Deezer, MusicBrainz and ListenBrainz and matched by ISRC
(`scripts/corpus/fetch_names.py`, snapshot `tests/fixtures/normalization/songs.json`).

| Source | Songs |
|---|---|
| Deezer charts US, GB, DE, FR, ES, IT, JP, KR | 20 each |
| Deezer chart BR | 12 (85 chart ISRCs not on MusicBrainz) |
| Random MusicBrainz recordings with an ISRC | 100 |
| Random MusicBrainz recordings with an RU ISRC | 20 (Deezer's "Top Russia" chart has 1 track) |
| **Total** | **292**; 318 MusicBrainz recordings, 312 with ListenBrainz metadata |

| Script | Songs |
|---|---|
| Plain Latin | 232 |
| Latin with accents | 35 |
| Cyrillic | 18 |
| Japanese / Chinese / Korean | 7 |

Also: 60 songs with several artists, 18 with a Deezer version suffix, 20 ISRCs with more than one
MusicBrainz recording.

Dropped and counted: 136 chart ISRCs missing from MusicBrainz, 103 duplicates across charts,
11 random MusicBrainz hits Deezer can't resolve.

## Do the sources give the same query?

With the decided rules, compared the way Soulseek compares words (case and punctuation
ignored; accents and script as written). Full report with every mismatch:
`tests/fixtures/normalization/report.md`.

| Source pair | Track | Main artist | Album |
|---|---|---|---|
| Deezer – MusicBrainz | 88.0% | 89.7% | 79.4% (any MusicBrainz release) |
| Deezer – ListenBrainz | 87.8% | 89.5% | 59.6% |
| MusicBrainz – ListenBrainz | 100% | 100% | 92.8% |

The rules barely move these numbers (no rules: 86.6% for Deezer–MusicBrainz track). The real
differences are not spelling:

| Difference | Example (Deezer → MusicBrainz) |
|---|---|
| Deezer romanizes or translates non-Latin names | Lee Eun Mi → 이은미; Vladimir Vysotsky → Владимир Высоцкий; With You (Album Version) → С тобой |
| Deezer puts version labels in titles | Vai Dar PT (Ao Vivo) → Vai dar PT; Dolphins (Live) → Dolphins |
| Different release picked | Ordinary: single "Ordinary" vs album "You'll Be Alright, Kid" |
| Credits written into titles | Stateside + Zara Larsson → Stateside |
| Credited differently | Spagna → Ivana Spagna; B-2 → Би-2 |

That's why names for slskd come from ListenBrainz after the song is picked on Deezer.

## slskd spot checks

On the main slskd, searches run one at a time. **Helpful peers** = peers with at least one audio
file of that artist (and that title, where one was searched). These are spot checks on a few
names, not a benchmark.

### Original script vs romanized

| Original | Helpful peers | Romanized / translated | Helpful peers |
|---|---|---|---|
| стрыкало | **45** | strykalo | 12 |
| Высоцкий | **61** | Vysotsky | 19 |
| Би-2 | **74** | B-2 | noise (5% of files on target) |
| с тобой би-2 | **21** | with you b-2 | 0 |
| 이은미 | **10** | Lee Eun Mi | 3 |
| 악동뮤지션 | 12 | AKMU | **62** |

The original script wins, except where a group's own Latin name is official (AKMU). Where a
source has both forms, both are worth a query step; search test v2 measures that.

### Accents

| Accented | Helpful peers | Plain | Helpful peers | Note |
|---|---|---|---|---|
| Café Tacvba | **127** | Cafe Tacvba | 79 | Most files keep the accent |
| Motörhead ace of spades | 560 | Motorhead ace of spades | 539 | Both hit the 600-response cap |
| Sigur Rós | 486 | Sigur Ros | 541 | The accented search hit the file limit first |
| Beyoncé halo | 0 | Beyonce halo | 0 | No responses at all; likely a server-filtered word |

Peers match accents loosely: the "Sigur Ros" query returned 9,574 "Rós" files. Where counts
differ, the original spelling wins, so queries keep it.

### Artist credit

Title + artist queries.

| Song | Full credit | All names, no join words | **Main artist only** |
|---|---|---|---|
| Jaded (Koe Wetzel feat. Ella Langley) | 4 | 11 | **24** |
| Dracula (Tame Impala with JENNIE) | 18 | 136 | **565** |
| Flores de abraços (Fito Páez & Moska) | 1 | 1 | **6** |
| Dead on the Dance Floor (Faber Drive feat. Proper Villains) | 2 | 2 | 2 |

Join words ("feat.", "with", "&") cut results the most. The Dracula main-artist count may
include the version without JENNIE; telling those apart is the matcher's job.

### Other findings

| Query | Result |
|---|---|
| Spagna / ivana Spagna | 217 vs 76 helpful peers: a short unambiguous artist name wins |
| vai dar pt ao vivo / vai dar pt | 0 vs 7: a version label in the query can zero a search |
| octopus's garden beatles / octopus garden beatles | 0 responses both, with or without the apostrophe: likely a server-filtered word |
| reality impala, paranoia impala, mistakes impala, duckworth kendrick | 90–100% of files on target, 276–588 helpful peers: title word + artist's last word works well |

## Not verified

- Spot checks only, on a handful of names; search test v2 (milestone 2) should measure name
  source and artist form at scale.
- Which words the Soulseek server filters is a guess from 0-response searches.
