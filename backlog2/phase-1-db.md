# Phase 1: DB redesign

Schema source: `agents_memory/topics/core-db-redesign.md` and `app/db/migrations/015_core_redesign.sql`.

## Tasks

- [x] `PRAGMA foreign_keys = ON` already set in `app/db/database.py` `connect()` (verified). Without it every
  FK is decorative (noted in the design doc).
- [ ] Review migration 015 against the design doc: `origins`, `catalog_searches`, `requests`,
  `slskd_searches`, `downloads`, `download_attempts`, `file_management`, `mb_provenance`, `peers`.
  Fix drift in the SQL, not in the doc.
- [ ] Check the handoff trigger `downloads_completed_handoff` in the migration matches the design
  doc exactly (pending stage, source from request, mbid to mb_provenance).
- [ ] Add the `failTag` and `unindexed` stages to `file_management.stage` (CHECK or app-side
  enum). Stage set: `pending | moved | failTag | unindexed | indexed | missing`.
- [x] SearchStore removed (`ca3f52a`); DownloadStore `insert_pending` / `import_pending` added (`ae627a3`).
- [ ] Stores: `request_store`, `download_store`, `file_store`, `peer_store`
  already exist as WIP. Bring each to the design: one method per table action, no SQL outside
  its store.
- [x] `tests/test_db` green: 78 passed, 0 failed (Docker).
- [ ] Tests: existing `tests/test_db` tests run green; add tests for the handoff trigger, the
  retry-count rule (`COUNT(*) - 1`), and FK cascade on `requests` delete.
- [ ] Ask-before-changing rule: any schema change beyond the design doc goes to open-questions.md
  first.

## Exit criteria

- `pytest tests/test_db -q` green
- A fresh DB built from migrations 001 to 015 matches the design doc table list
- Handoff trigger covered by a test that inserts a request, a search and a download, then sets
  `state='completed'` and checks the `file_management` row

## Subagents

- One agent, medium reasoning. Scope: `app/db/` and `tests/test_db/` only.
- It commits on `core-rebuild` after each green test group.

## Status (2026-10-19)

- tests/test_db: 78 passed, 0 failed.
- Full suite (excluding tests/live): 861 passed, 51 failed. The 51 are in test_download_monitor, test_history_cleaner and test_download_routes and belong to phase 4.
