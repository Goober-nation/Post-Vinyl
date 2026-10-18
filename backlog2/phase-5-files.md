# Phase 5: file lifecycle and Navidrome link

Design source: "File identity = Navidrome's media_file.id" and the `file_management` draft in
`agents_memory/topics/core-db-redesign.md`.

## Stages

`pending` (handoff done) → `moved` (placed under library root) → `unindexed` (placed, waiting for
Navidrome) → `indexed` (`navidrome_id` set).

Failure stages (new, from the board):
- `failTag`: the tagger failed. The file stays where it is and the row keeps `placed_path` NULL.
- `missing`: the file is gone from `downloads/complete/soulseek/<username>/`.

## Tasks

- [ ] Tagging: decide the tagger path (existing beets service, `app/services/beets.py`, versus a
  new call). Ponytail rung 5 first: beets is already installed and used. Reuse it.
- [ ] Placement: `placed_path` is relative to the library root; collision rename `track (1).flac`
  is kept.
- [ ] `failTag` on tagger error; test it with a forced beets failure.
- [ ] Navidrome link: after a scan, resolve `navidrome_id` by path or filename via the Navidrome
  API (`agents_memory/topics/navidrome-api.md`). Mark `indexed` only when found.
- [ ] `unindexed` retry: a periodic check (interval in config) for rows still `unindexed`.
- [ ] Temporary path table for files that are in flight but not yet indexed (board stage 5).
- [ ] After handoff: delete the `downloads` row and remove the transfer from slskd
  (`?remove=true`). HistoryCleaner becomes the delete-handed-off pass (design doc).

## Exit criteria

- Test: tagger failure → `failTag`, file untouched
- Test: placed file → `unindexed` → `indexed` after a stubbed Navidrome lookup
- Live run: a real file placed in the test library, indexed by the Docker Navidrome instance

## Subagents

- One agent, high reasoning (touches the library on disk). Scope: `app/services/library.py`,
  `app/services/navidrome_library.py`, `app/workers/history_cleaner.py`, tests.
- Never writes to a real library path. Test library root only.
