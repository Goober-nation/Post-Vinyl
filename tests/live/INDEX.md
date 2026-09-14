# Live suite index

Read this before opening any file in `tests/live/` to decide what to run.
The suite is large (118 tests across 12 files) and a full run costs real
download budget and 20+ minutes; this file exists so a targeted question
("does the album-consolidation logic work?", "is search timing broken?")
can be answered by running a handful of tests instead of the whole thing —
without first reading every file to find them.

## Cost tiers (pytest markers)

Every test in `tests/live/` carries exactly one of these (registered in
`conftest.py`, enforced by nothing but convention — keep it that way when
adding new tests):

| Marker | Meaning | Run cost |
|---|---|---|
| `no_downloads` | Search-only, synthetic fixtures, or pure API/DB reads. Queues zero real Soulseek downloads. | Seconds to ~1 min. Safe anytime. |
| `needs_existing_corpus` | Reads state a prior download wave must have already produced (beets rows, files on disk, Navidrome index). Does not itself queue new downloads, but is vacuous or skips without that corpus present. | Fast once the corpus exists; useless without it. |
| `queues_downloads` | Queues real Soulseek downloads and consumes download budget. | Slow, network-dependent, subject to the socket-tax cost below. |

Run just one tier:

```bash
python3 -m pytest tests/live --live -m no_downloads -v
python3 -m pytest tests/live --live -m needs_existing_corpus -v
python3 -m pytest tests/live --live -m queues_downloads -v
```

Combine with `-k` or a specific file/class/test path exactly like any other
pytest run — the marker just narrows the starting set.

**Soulseek socket-tax warning** (see `agents_memory` / prior session
findings): each real search costs ~6-7k host sockets and pins Docker's port
forwarder. Prefer `no_downloads` or `needs_existing_corpus` tests whenever
they answer the question — reach for `queues_downloads` only when the thing
under test genuinely requires a fresh transfer.

## File-by-file

| File | Tier | Covers | Related issues / P-codes |
|---|---|---|---|
| `test_album_consolidation.py` | `no_downloads` | Album grouping/consolidation: sequential-import race, albumartist-casing mismatch, sweep/per-import parity, `resolve_canonical` artist mismatch. Uses freshly-generated synthetic audio (not real downloads) staged via `docker compose exec`. | #2 |
| `test_query_anomaly.py` | `no_downloads` | Search-term anomalies (musica vs. slskd result-count mismatches), zero-result sweeps over common names. Explicitly documented as "queue nothing, cost no download budget." | #10, #12, #17 |
| `test_p68_musicbrainz.py` | `queues_downloads` | `TestMusicBrainzSearch` (search behavior) + `TestMusicBrainzDownload` (MB-initiated download routing, P6.8). The whole file is marked `queues_downloads` conservatively; `TestMusicBrainzSearch` alone may not need real downloads — check before assuming. | #4 (library-profile routing), #12 |
| `test_p65_4_persistence.py` | `queues_downloads` | Header/state persistence across musica and slskd restarts (Case A/B), worker-state persistence. Queues real downloads to have something to persist. | — |
| `test_p65_5_queue_priority.py` | `queues_downloads` | Manual downloads take priority over rec queueing; manual is never blocked; stale-pending edge case. | #5 (adjacent — manual-priority interacts with periodic pulls) |
| `test_p65_6_query_pipeline.py` | `queues_downloads` | Search query ladder/word-cap behavior, manual search path. | #10, #17 |
| `test_scenarios.py` | `queues_downloads` | The U1-U10 end-to-end user journeys (manual pipeline, each rec category, duplicate download, peer failure, crash recovery, stale row, playlist lifecycle, concurrency). Heaviest tier — full journeys, not single assertions. | broad — most stability-pass issues surface here first |
| `test_stages_import.py` | `needs_existing_corpus`\* | S7 (beets import) / S8 (tags correct) / S9 (placement correct) / S10 (dedup: same-track-twice, cross-tree, stale-row). \*S7-S9 actually *do* queue real downloads via the `track`/`stack` fixtures — only S10 is truly download-free (donor + staging, like `test_album_consolidation.py`). Marked at file level for now; treat S7-S9 as `queues_downloads` in practice. | #2 (S10 is the direct precedent this repo's #2 tests followed), #4 |
| `test_stages_library.py` | `needs_existing_corpus` | S11 (Navidrome indexed) / S12 (playlist correct) / S13 (user can find it). Reads state a download wave already produced; queues nothing new itself. | #4/#19 (playlist creation), #6 |
| `run_suite.py` | n/a (orchestrator) | Not a test file — the serialized, budget-aware driver for a full run across everything above. Use this only when you actually want the full statistical picture, not for a targeted question. | — |

## Adding a new live test

1. Pick the narrowest cost tier that's actually true — default to
   `no_downloads` if you can get away with synthetic fixtures or staged
   files (see `test_album_consolidation.py` for the pattern: generate
   audio + tags in-container via `docker compose exec`, no P2P needed).
2. Add `pytestmark = [pytest.mark.<tier>]` at true module level, **after**
   the real top-level import block — not inside a triple-quoted
   in-container driver-script string. Double-check with
   `python3 -c "import ast; ast.parse(open(path).read())"` if you scripted
   the insertion.
3. Add a row to the table above.
