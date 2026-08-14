# Live-stack tests (Track 2)

These drive the **real** Docker stack. They queue real downloads from real
Soulseek peers, restart your containers, and read the live database. Nothing
here is mocked.

They are skipped by default — `pytest tests/` stays safe on any machine. You
have to opt in with `--live`.

## Prerequisites

The stack must already be up, with slskd connected to the Soulseek network:

```bash
docker compose up -d
```

**The running image must actually contain the code you're testing.** A
container built before P6.5-4 runs fine without migration 004, so every
persistence test would fail with a bare `no such table` that reads like a
harness bug. The session fixture checks for this and tells you to rebuild:

```bash
docker compose up -d --build musica
```

Sanity-check before running anything:

```bash
curl -s localhost:8092/api/system/status | python3 -m json.tool
```

Some tests need a rec category enabled (`recs.comfort_zone_enabled` etc. in
`config/config.toml`, plus ListenBrainz credentials). They `skip` rather than
fail when recs are off, so a run with recs disabled still exercises everything
in P6.5-4.

## Running

```bash
python3 -m pytest tests/live/ --live -v
```

One file at a time, which is usually what you want — a full run restarts
containers several times and can take 20+ minutes:

```bash
python3 -m pytest tests/live/test_p65_4_persistence.py --live -v
```

Keep the timelines somewhere you can read them afterwards:

```bash
python3 -m pytest tests/live/ --live -v --artifacts ./live-artifacts
```

Options: `--musica-url` (default `http://localhost:8092`), `--db-path`
(default `./app_data/musica.db`), `--artifacts`.

## What each file covers

| File | Covers |
|---|---|
| `test_p65_4_persistence.py` | Responses/jobs/worker-state survive a restart; Case A (musica-only restart leaves slskd alone) vs Case B (slskd forgets → orphan → retry without re-searching) |
| `test_p65_5_queue_priority.py` | Rec queueing pauses for manual transfers and resumes; manual is never blocked; the stale-pending deadlock found in review |
| `test_p65_6_query_pipeline.py` | Word cap, ladder early-stop, pull wall-time, manual-search path |
| `collect_pass_ratios.py` | Repeated-run data collection for tuning `search.pass_ratio_threshold` |

## Reading a failure

Every run writes `timeline.jsonl` — one timestamped line per API call, SSE
event, docker action, and explicit marker, on a single monotonic clock. When a
timing assertion fails, that file tells you the order things actually happened
in, which is almost never reconstructable from the assertion message alone.

```bash
python3 -c "
import json,sys
for line in open('live-artifacts/<test-name>/timeline.jsonl'):
    e = json.loads(line)
    print(f\"{e['t']:8.2f}  {e['kind']:10}  {e.get('event') or e.get('label') or e.get('path','')}\")
"
```

## Two things worth knowing before you trust a result

**Restarting musica is not the interesting case.** `docker compose restart
musica` leaves slskd transferring untouched, so the rows just resync — that
path was never broken. The case that *was* broken is slskd forgetting the
transfer, which is why `TestCaseB` restarts slskd instead. If you only ever run
Case A you'll "verify restart persistence" without testing anything.

**Result counts are not assertions.** Which peers answer depends on who is
online. Tests assert on *mechanism* (word counts, event ordering, whether a
search fired) and only *report* outcomes. `search.response_threshold` (10) also
cancels each search as soon as 10 peers answer, so every pass ratio is computed
over a ~10-peer sample regardless of how popular the track is — small samples,
high variance. That's what `collect_pass_ratios.py` is for: run it several
times and look at the spread, not any single number.

## Known live behaviors this harness works around

**Keep-alive races.** uvicorn closes idle connections after ~5s and
`requests.Session` reuses pooled connections without retrying, so a gap
longer than that between calls — which pytest setup/teardown easily produces
— lands a request on a socket the server is closing. It shows up either as a
`RemoteDisconnected` or as a request that never reaches the route handler at
all (the handler's own first log line never appears). `MusicaClient._call`
retries these and records each one as `api_retry` in the timeline, so a run
that needed several retries is still visible rather than silently smoothed
over. If you see a lot of them, that's worth investigating on its own.

**`GET /api/searches/{id}` can block for minutes.** `search.wait_seconds`
(10) bounds only the poll loop. The flush that follows —
`SlskdSearch._fetch_responses` — retries 10 times with a 15s HTTP timeout
each, so a slow slskd can hold the request for ~155s, while the code's own
log line claims "Responses not available after 5s". `search_detail()`
defaults to a 180s timeout for that reason.

## Cleaning up after a run

These tests queue real downloads. To clear what they left behind:

```bash
curl -s -X DELETE localhost:8092/api/transfers
```

That only removes finished rows. Anything still transferring, cancel from the
UI or let it finish.
