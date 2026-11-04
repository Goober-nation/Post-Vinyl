# First search benchmark

Research from 2026-10 (#34, #63, #64). This run was later judged flawed and replaced by
[search test v2](search-strategy.md), which set the query order. It's kept for what still stands.

## What still stands

**A response cap of 600** gave about 40–70% more helpful peers than 300 on every query shape, at the
same deadline.

**No early stop on a low response count.** Niche or long queries keep collecting until the end, so a
3 s stop cuts exactly those off. The stable-count exit replaced it.

**LW (album word + artist word) is weak for tracks**: other albums by the same artist fill the cap.
It's the choice for whole-album searches, if those are built.

## Superseded numbers

For history only. Shapes: L0 as today, T full title.

| Benchmark (600 cap) | Found | Median helpful peers | Avg queries per song |
|---|---|---|---|
| Album tracks: L0, then T | 99% | 180 | 1.09 |
| Album tracks: L0 alone | 91% | 150 | 1.00 |
| Singles: T, then L0 | 96% | 34 | 1.04 |
| Singles: L0, then T | 96% | 28 | 1.20 |

## Corpus

- Seeds: `tests/fixtures/corpus/seeds.toml` (114 seeds including 8 negative traps) and `singles.toml`
  (25 singles on no studio album), resolved through MusicBrainz.
- Every query was recorded raw, with no matching or early stop: 812 queries at a 300 cap, 529 at 600,
  124 for singles.
- Scoring: `python -m scripts.corpus.label --corpus <dir> [--plan <plan.json>]`.

**Gotcha**: two slskd instances share Soulseek's listen port, so never run both. A multi-line YAML
`command:` silently dropped harvester flags.
