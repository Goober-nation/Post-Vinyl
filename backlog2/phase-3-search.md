# Phase 3: slskd search

Research sources: `../post-vinyl-research/telegram-slskd-local-bot` (scorer, query ladder, stable
exit), `slsk-batchdl` (boundary matching, sorting), `soulbeet` (word-set matching, weighted total).

## Tasks

- [ ] **Stable-count exit**: stop polling when the file count is unchanged for `search.stable_window_secs`
  (default 8), with `search.poll_interval_secs` (2) and `search.min_wait_secs` (5) also configurable.
  Hard timeout stays `search.timeout_secs`. Config layers all five.
- [ ] **Query ladder** (reuse `app/services/query_builder.py` and `track_requester.run_ladder`):
  artist + cleaned title, then title only, then reduced queries with year, then artist + Latin
  keywords. Pass threshold `search.pass_ratio_threshold` already exists (0.75).
- [ ] **Title cleaning rules** as a configured list (`search.noise_patterns`), not inline regexes.
  Each rule has a test with a real title that must survive (e.g. `Party Like It's 1999`).
- [ ] **Ranking weights** as config: duration, quality tiers, slot, speed, queue, filename
  relevance. Defaults match the current scorer so behaviour is unchanged on first land.
- [ ] **Title guard** (drop copies whose name shares no word with the title), tunable on/off.
- [ ] **Boundary matching** for the filename check (slsk-batchdl style), with a substring fallback
  only for terms of at least `search.substring_min_len` characters, lower weight.
- [ ] Search results keep the real slskd `search_id` (design rule: never `cache-*` ids).

## Exit criteria

- Unit tests per rule and per weight; a golden test set of 20 real titles from the live library
- Live run against the Docker slskd test instance: query ladder returns candidates, stable exit
  fires, time per search recorded
- Changing any value in `config.toml` changes behaviour without a code edit (test)

## Subagents

- Research-only pass first: low reasoning, reads the three reference repos, writes
  `backlog2/notes/search-research.md`. No code.
- Implementation: medium reasoning, scope `app/services/search*`, `query_builder.py`,
  `track_requester.py`, config layers, tests.
