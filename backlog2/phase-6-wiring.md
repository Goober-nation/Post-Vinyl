# Phase 6: wiring

Goal: routes and workers call the new core services, nothing else.

## Tasks

- [ ] Routes in `app/routes/` (search, queue, library) call the phase 2 interfaces only
- [ ] Workers (`download_monitor`, `history_cleaner`, `mb_resolver`) use the new stores
- [ ] Remove the old search and download paths once the new ones pass phase 7
- [ ] Config UI (`app/static/js/config.js`) shows the new tunables (layer 5 of the config rule)
- [ ] API contract updates in `agents_memory/topics/api-contract.md`, and `docs/api.md`

## Exit criteria

- `pytest tests/ -q` green on the branch
- Route tests cover the new status codes (no 200 with an error body)

## Subagents

- Medium reasoning, one agent per route group, each on its own files. Groups: search, queue,
  library, config. They do not share files.
