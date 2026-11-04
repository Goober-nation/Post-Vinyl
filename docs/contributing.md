# Contributing

Read [architecture](architecture.md) first. `AGENTS.md` has the extra rules for AI agents.

## Setup

Always in a virtual environment.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

To run the app you need a Navidrome, an slskd and these values:

```bash
PV_DEV=1 PV_DB_PATH=./pv.db PV_CONFIG_PATH=./post-vinyl.yaml PV_NAVIDROME_URL=http://localhost:4533 PV_SLSKD_URL=http://localhost:5030 PV_SLSKD_API_KEY=key .venv/bin/uvicorn app.main:create_app_from_env --factory
```

Copy `post-vinyl.example.yaml` to `post-vinyl.yaml` first. `/docs` then serves Swagger.

## Rules

Stop at the first rung that holds: does this need to exist, is it already here, standard library,
platform feature, installed dependency, one line. Only then write the minimum that works. Never trim
trust-boundary validation, data-loss handling, security or accessibility.

- Follow the [layers](architecture.md#layers): no SQL outside `stores/`, one client per system, pure
  `core/`.
- State moves go through `core/states.py`.
- Never duplicate state another system owns.
- Only user-facing or advanced values become settings, with self-explanatory names.
- No type suppressions: fix the cause.
- The repo is public: no secrets, server addresses, account names or personal paths.

**Music rules**: tag only what's known to be true. Never download an excluded version unless the
requested song is one.

**Outside services**: a claim about a service isn't a fact until it's checked against its source at
the running version, and confirmed with a live call. Record findings in
[outside services](outside-services.md).

## Testing

```bash
pytest tests/ -v
```

```bash
ruff check app/ scripts/
```

```bash
ruff format --check app/ scripts/
```

```bash
mypy app/
```

Test where it pays off: matcher, query strategy, normalization, state transitions, stall rule, DB
triggers and cascades, auth. Skip tests that only restate the code.

| Kind | Where |
|---|---|
| Unit and DB | `tests/test_core`, `test_stores`, `test_services`, `test_clients`, `test_routes`. Stores use real SQLite, clients and services use fakes |
| Fakes | `tests/test_services/test_fakes_match.py` runs one scenario set on real service and fake |
| Clients | `tests/test_clients/test_catalog_contract.py` runs one contract over every `CatalogSource` |
| API doc | `tests/test_routes/test_api_doc.py` fails when `docs/api.md` and the app differ |
| Live | `tests/live/`, skipped unless `PV_LIVE=1` with real services |

Run live tests on an isolated stack with its own test accounts, never against real data. A Soulseek
account can't be logged in twice, so don't run two stacks on one. Integration changes need a run
against the real service.

Done means tests pass, `ruff` and `mypy` are clean for touched files, a real-service run is recorded
for integration changes, and anything unverified is said to be.

## Adding a source

No service, core or route code names a concrete source.

1. Add a class behind the existing abstract base (`CatalogSource`, `DownloadSource`), plus a fake.
2. Build it in `build_core`.
3. Add its name to `KNOWN_SOURCES` in `app/config.py`, and to the comment in
   `post-vinyl.example.yaml`, in the same change.

Backend-specific tables get a prefix and hang off `downloads` or `requests`. Core sees only the
generic rows.

## Adding a setting

Add a field with `setting(...)` to the dataclass in `app/config.py`, and document it in
`post-vinyl.example.yaml` and [configuration](configuration.md). The schema and the settings screen
follow.

## Database changes

Add `app/stores/migrations/NNN_name.sql` with the next number. Index every foreign key. If a new
state is added, update the `CHECK` and `core/states.py` together. Keep SQL in `stores/`, and return
frozen dataclasses.

## Endpoints

Add the route, list it in [api.md](api.md), add any new exception to `STATUS` in
`app/routes/errors.py`, and regenerate the frontend types ([how](frontend.md#commands)).

## Research tooling

`scripts/corpus/` is the search benchmark: seeds, harvest, labelling, reports. Fixtures are in
`tests/fixtures/`. `plan.py` and `resolve_seeds.py` still import code that no longer exists, so they
don't run until ported.

Benchmarks run on a separate search-only slskd with its own Soulseek account, sharing no listen port
with a running one. Log counts, not pass or fail, and keep each slskd search id so a search can be
re-read. Seed names come only from official catalog data.
