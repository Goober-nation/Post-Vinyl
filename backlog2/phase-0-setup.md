# Phase 0: setup and baseline

Goal: a reproducible environment and a recorded test baseline before any new code.

## Tasks

- [x] Create local branch `core-rebuild` from `dev`
- [x] Commit the existing WIP redesign (migration 015 and new stores) as a baseline
  (`c5491b3`). The untracked backup DB `musica_pre_013_014_backup.db` stays out of git.
- [x] Create private project board "Post-vinyl core service (backlog2)"
- [x] Docker daemon started (Docker Desktop, 29.8.2)
- [ ] Test runner container: `python:3.12-slim`, repo mounted read-only, copied to `/work`,
  `pip install -r requirements.txt pytest`, then `pytest`. Host venv not used for testing.
- [x] Baseline in Docker, `pytest tests/test_db -q` on `c5491b3`: **66 passed, 10 failed**.
  All 10 failures are in `tests/test_db/test_search_store.py`: they query the `searches` table,
  which migration 015 drops (replaced by `slskd_searches`). The store itself is stale WIP, so
  phase 1 must either port it to `slskd_searches` or remove it. Not yet fixed.
- [ ] Baseline for the rest of `tests/` and for `ruff check app/` and `mypy app/` (not yet run)
- [ ] Read `agents_memory/topics/naked-clone-testing.md` and confirm the test stack
  (`docker-compose.test.yml`, `.env.test`) starts without touching `.env`
- [ ] Decide issue location (see open-questions.md, Q1): the repo is public, so issues are public

## Exit criteria

- Baseline numbers written into this file (pass/fail counts, ruff and mypy counts)
- Test stack start command recorded, with the exact compose command used

## Notes

- Pre-existing ruff/mypy violations are known (MEMORY.md). Only new violations in touched files
  count against a phase.
- Subagent: none. This is one sequential setup pass.
