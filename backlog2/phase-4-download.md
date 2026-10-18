# Phase 4: downloads

Design source: "One downloads row per requested file" and "One retry implementation" in
`agents_memory/topics/core-db-redesign.md`.

## Tasks

- [ ] Every queue call writes the `downloads` row (with `request_id`, `search_id`) **before**
  calling slskd. Rows are always inserted as `queued`.
- [ ] Retry updates the same row (new `username`, `filename`, `slskd_id`, state back to `queued`)
  and records each peer in `download_attempts`. Peer selection skips every peer already in it.
- [ ] Retry ceiling `download.max_retries_per_track` (config, default from current code).
- [ ] Stalled-transfer rule: no start in `download.start_timeout_secs` or speed under
  `download.min_speed_kbps` moves the peer to failed. Both values are config, default to be
  tuned later; the first test run uses very forgiving values.
- [ ] Monitor (`app/workers/download_monitor.py`) uses the DB-backed retry only. The old
  in-memory retry is removed. The UI Retry button calls the same path.
- [ ] Peer records: failures go to `peers` (only peers with a current bad record).
- [ ] Ignore unmatched slskd transfers (design rule: the app tracks only transfers it started).

## Exit criteria

- Test: three dead peers in a row exhaust the budget and mark the download failed, no peer tried
  twice
- Test: a download reaching `completed` creates exactly one `file_management` row
- Live run against the Docker slskd test instance with a known peer, plus a forced failure

## Subagents

- One agent, medium reasoning. Scope: `app/db/download_store.py`, `app/services/download.py`,
  `app/workers/download_monitor.py`, `tests/test_workers/`. Runs after phase 2 lands.
