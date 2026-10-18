# Phase 7: validation

Goal: prove the core loop works on real services, in the isolated Docker test stack.

## Rules

- Use `docker-compose.test.yml` and `.env.test` only. Never the real `.env` or `docker-compose.yml`.
- Run long tests detached (background), check them periodically. A test that runs too long
  gets a shorter replacement test, not an unchecked wait.
- Record each run in `backlog2/notes/validation-log.md`: date, command, pass/fail counts,
  anything skipped and why.

## Tasks

- [ ] Start the test stack; record the start command and health checks
- [ ] End-to-end: request → search → download → file_management → placed → indexed
- [ ] Failure paths: dead peers exhaust budget; tagger failure gives `failTag`; Navidrome down
  gives `unindexed`, then recovers
- [ ] Forgiving thresholds run first; a tuned run follows once values are agreed
- [ ] Report: what is verified against real services, what is only unit-tested, what is unverified

## Exit criteria

- End-to-end run recorded with timings
- The report separates verified from unverified items, as AGENTS.md requires
