# Backlog 2: core service and DB redesign

Index for the core-rebuild plan. Phase files sit next to this file. Live status lives in the
GitHub project board (private): https://github.com/users/Goober-nation/projects/4

## Goal

Rebuild the core loop as one clean service: **request → slskd search → download → file lifecycle
→ Navidrome link**. Everything else (recs, beets profiles, UI) talks to it through interfaces.

Design source of truth: `agents_memory/topics/core-db-redesign.md` (settled decisions there are not
re-litigated here). Reference research: `../post-vinyl-research/` (not part of this repo).

## Rules for every phase

- **Branch:** `core-rebuild`, local only, off `dev`. Commit often. **Never push.**
- **Reuse first** (ponytail ladder, see below). Do not re-invent what a library or the existing
  code already does.
- **No hardcoded tunables.** Thresholds, timeouts, weights and regex rule lists go in
  `config.toml` through the five config layers listed in `agents_memory/MEMORY.md`.
- **Tests run inside Docker, not on the host.** Unit and DB tests run in a throwaway
  `python:3.12-slim` container (same base as the Dockerfile) with the repo mounted read-only.
  Live-service tests run in the isolated test stack (`docker-compose.test.yml`, `.env.test`).
  Never run compose or setup flows against the real repo `.env`.
- **Long tests run detached.** Check them periodically; replace a test that runs too long with
  a shorter one rather than waiting.
- **Done means:** tests pass, ruff and mypy clean for touched files, and a real-service run is
  recorded for any integration change. Unverified items are marked as such, never ticked.

## Ponytail ladder (applied at every design and code decision)

Source: DietrichGebert/ponytail, `skills/ponytail/SKILL.md` (MIT). Stop at the first rung that
holds:

1. Does this need to exist?
2. Is it already in this codebase?
3. Does the standard library do it?
4. Is there a native platform feature?
5. Is there an installed dependency?
6. Can it be one line?
7. Only then: the minimum that works.

Never trim trust-boundary validation, data-loss handling, security or accessibility.

## Phases

| # | File | Scope | Depends on |
|---|------|-------|------------|
| 0 | [phase-0-setup.md](phase-0-setup.md) | branch, venv, baseline tests, test stack | — |
| 1 | [phase-1-db.md](phase-1-db.md) | migration 015 finished, stores, FK pragma, triggers | 0 |
| 2 | [phase-2-interfaces.md](phase-2-interfaces.md) | ABC service interfaces for the core loop | 1 |
| 3 | [phase-3-search.md](phase-3-search.md) | slskd search, stable-count exit, title cleaning, ranking | 2 |
| 4 | [phase-4-download.md](phase-4-download.md) | transfers, peer failover, retry on the same row | 2, 1 |
| 5 | [phase-5-files.md](phase-5-files.md) | handoff, placement, `failTag` / `unindexed`, Navidrome link | 4 |
| 6 | [phase-6-wiring.md](phase-6-wiring.md) | routes and workers wired onto the new core | 3, 4, 5 |
| 7 | [phase-7-validation.md](phase-7-validation.md) | live tests on the Docker stack, reports | 6 |

Open questions and decisions still needed from the user: [open-questions.md](open-questions.md).

## Track state machine (from the board, stages 1-5)

- Request: `requested` → `dropped` | `reRequested`
- Search: `searching` → `found` | `notFound`
- Download: `midDownload` → `downloaded` | `notDownloaded` | `badDownload`
- Tag: `tagged` | `failTag` (new: tagger failed)
- Index: `unindexed` (new: placed, waiting for Navidrome) → `indexed`

Board names and DB stages are kept in sync when a phase lands. Stage 3 thresholds start very
forgiving for testing; tuning comes later.

## Subagent use

Implementation work can run in Haiku 5.5 subagents at low, medium or high reasoning effort, as
the phase file says. Subagents never touch the same files at the same time, and each one commits
on `core-rebuild` only.
