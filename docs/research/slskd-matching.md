# How peers name files: data for the slskd matcher

Research for milestone 1, stage 2 (#72), 2026-10-27. Measures how peers lay out and spell file
paths, so the matcher (milestone 5) rests on data. How queries are built is in
[normalization.md](normalization.md).

## Data

- **Files**: raw responses from the first search harvest (#34): 1,096 queries over three
  harvests, copied read-only into a local cache. Only counts are published here, no peer
  names or paths.
- **Seeds**: 94 old track seeds with a MusicBrainz id, minus the `library` and `negative`
  buckets; names re-fetched from MusicBrainz. 90 seeds have counted files.
- **A file counts** when it's audio, within ±3 s of the MusicBrainz length, its filename has
  every title word, and the main artist is somewhere in the path (accents, case and
  punctuation folded).
- **Totals**: 44,968 counted files.

Code: `scripts/corpus/path_layout.py`; generated report:
`tests/fixtures/normalization/paths_report.md`.

### Checking the counting rule

| Rule | Random 100 checked | Wrong songs | Excluded versions |
|---|---|---|---|
| Half the title words + length (first try) | 35 had no artist in the path | ~33 of those 35 (e.g. Dokken "In My Dreams" for Fleetwood Mac "Dreams") | not checked |
| **Every title word + artist in path + length** | 100 | **0** | 3–4 (a remix, a radio edit, a Peel session, possibly a live file) |

Excluded versions are the matcher's filter to apply, so they don't distort the layout numbers.

## Files seen

| Reason | Files |
|---|---|
| Length more than 3 s off | 4,130,815 |
| Not audio | 898,865 |
| No length reported | 142,545 |
| Missing a title word | 84,140 |
| Artist not in path | 14,431 |
| **Counted** | **44,968** |

## Where each name appears

Out of 44,968 counted files. The location columns show the nearest folder level holding the
name.

| Field | Filename | Parent folder | Two folders up | Higher | **Anywhere in the full path** | Not in the path |
|---|---|---|---|---|---|---|
| Title | 44,777 (99.6%) | 2 (0.0%) | – | – | **44,779 (99.6%)** | 189 (0.4%) |
| Main artist | 19,314 (43.0%) | 6,313 (14.0%) | 16,599 (36.9%) | 2,742 (6.1%) | **44,968 (100%)**, by the rule | 0 |
| Album | 5,746 (12.8%) | 19,677 (43.8%) | 1,345 (3.0%) | 40 (0.1%) | **26,808 (59.6%)** | 18,160 (40.4%) |
| Title + artist + album together | | | | | **26,657 (59.3%)** | |

- **Album** counts any MusicBrainz release title except one named like the track (a single
  would otherwise "find" the album in every filename). 738 files belong to seeds with no other
  release, so among files with an album to find the rate is 26,808 / 44,230 = **60.6%**.
  The missing 40% are mostly singles, compilations and playlist folders.
- The typical layout is `Artist\Album\NN - Title.ext`: the artist two folders up, the album in
  the parent folder, the title in the filename.

## Filename and folder patterns

A file can have several.

| Pattern | Files | Share |
|---|---|---|
| Year in a folder | 25,697 | 57.1% |
| "Artist - Title" filename | 16,838 | 37.4% |
| Format tag in a folder (FLAC, 320…) | 8,159 | 18.1% |
| Disc folder (CD1…) | 3,201 | 7.1% |
| `_` instead of spaces | 1,313 | 2.9% |
| `.` instead of spaces | 12 | 0.0% |

## Track number at the start of the filename

| Form | Files | Share |
|---|---|---|
| `01 - ` | 12,966 | 28.8% |
| none | 11,318 | 25.2% |
| `01 ` | 9,676 | 21.5% |
| `01. ` | 6,872 | 15.3% |
| `1-01` (disc-track) | 2,228 | 5.0% |
| `01_` / `01-` | 1,777 | 4.0% |
| `A1` (vinyl side) | 131 | 0.3% |
| **Any track number** | **33,650** | **74.8%** |

## Spelling vs the MusicBrainz name

Least folding needed for the name to appear as one phrase.

| Field | As written | + case | + case, accents | + case, accents, punctuation | Not found as one phrase | **Found after full folding** |
|---|---|---|---|---|---|---|
| Title (in filename) | 36,063 (80.2%) | 5,790 (12.9%) | 307 (0.7%) | 2,623 (5.8%) | 185 (0.4%) | **44,783 (99.6%)** |
| Main artist (full path) | 37,973 (84.4%) | 1,854 (4.1%) | 1,151 (2.6%) | 3,990 (8.9%) | 0 | **44,968 (100%)** |

Folding case, accents and punctuation on both sides covers almost every right-song file the
exact name misses: the matcher should fold all three.

### Non-Latin seeds

Paths of counted files for non-Latin seeds are in the same script as the seed (Cyrillic 220,
katakana 233, hiragana 73, Hangul 17). This says nothing about romanized paths: the rule needs
the artist as written, so a Latin-script path for a Cyrillic artist can't count.

## What the matcher should take from this

| Finding | For the matcher |
|---|---|
| Title is in the filename 99.6% of the time | Match the title against the filename only. |
| Artist is spread over the filename, parent, two folders up | Match the artist against the full path. |
| Album is in the path only ~60% of the time | Album can raise confidence but must not be required. |
| 75% of filenames start with a track number, in six forms | Strip leading `NN - `, `NN. `, `NN `, `N-NN`, `NN_`, `A1` before comparing. |
| 37% "Artist - Title" filenames | Remove the artist from the filename before checking for extra words. |
| 80% of titles match as written, 99.6% after folding | Fold case, accents and punctuation on both sides. |
| Years, format tags and disc folders are common in folders | Ignore them in folder names. |

## Not verified

- **Paths with no artist at all**: a hand check of the looser rule found about 2 in 100
  right-song files without the artist (e.g. a "Grammy Nominees" compilation).
- **The artist spelled another way** (Utada Hikaru for 宇多田ヒカル, a shortened band name):
  the rule needs the artist as written, so these aren't counted.
- **Weighting**: counts are per file, so popular seeds dominate.
- **Old harvest**: the files come from searches built with the old query shapes, not the
  rules in normalization.md.
