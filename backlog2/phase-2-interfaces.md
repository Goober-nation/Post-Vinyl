# Phase 2: core interfaces

Goal: the core loop is reachable only through ABCs (MEMORY.md: explicit ABCs, not Protocols).

## Interfaces

- `SearchService`: `search(request) -> list[Candidate]`, raises on slskd unreachable
- `DownloadService`: `start(candidate) -> download_id`, `status(download_id)`, `retry(download_id)`
- `LibraryService` (file lifecycle): `place(file_management_id)`, `index(file_management_id)`
- `RequestService`: `create(source, query, mbid)`, `resolve(request_id)`

Existing interfaces in `app/services/interfaces/` (download, search, musicbrainz) are the starting
point. Extend them, do not add parallel ones (ponytail rung 2).

## Tasks

- [ ] Read existing interfaces and `app/services/download.py`, `search.py`, `library.py`
- [ ] Define ABCs with docstrings stating the state each call may leave a row in
- [ ] Add `ConfigError` style exceptions in `app/exceptions.py`, one per failure class
- [ ] Wire `app/services/bootstrap.py` to build the services from config
- [ ] Tests: each interface has a fake implementation in `tests/` used by the phase 3-5 tests

## Exit criteria

- Services import without starting slskd or Navidrome
- Fake implementations satisfy every ABC method (test per interface)

## Subagents

- One agent, high reasoning (interfaces shape everything after). Scope: `app/services/interfaces/`,
  `app/services/bootstrap.py`, `app/exceptions.py`, `tests/test_services/`.
