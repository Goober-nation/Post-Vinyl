# AGENTS.md: Post-Vinyl AI guide

Read this first. Post-Vinyl is being **rebuilt from scratch** (since 2026-10-27). The design lives in
`agents_memory/rebuild/`; where anything else (old issues, old notes, `legacy/` code) disagrees with
it, the rebuild notes win.

---

## Session startup

1. Read this file.
2. Read `agents_memory/MEMORY.md` (local only, may be missing on another machine).
3. Read `agents_memory/rebuild/architecture.md`, then `rebuild/work-plan.md`. Read the other rebuild
   notes only when the task touches them:
   - `core-db-redesign.md`: schema (built + planned)
   - `users-auth.md`: login, sessions, default deny
   - `design-notes-compilation.md`: search & matching decisions
   - `search-test-v2.md`: normalization test + search benchmark rerun
   - `live-runs-log.md`: past runs on the test stack
4. Check the current GitHub milestone and its issues (`gh issue list --milestone "<title>"`).
5. Check the branch (`git branch --show-current`); see Branches below.
6. If the user gives a free-form idea mid-session, say which milestone/issue it belongs to (or that it
   needs a new one) before acting.

If anything is unclear or missing, ask before proceeding.

---

## Current state

- **`app/`**: the new app, an empty layered skeleton (`clients/`, `stores/`, `core/`, `services/`,
  `workers/`, `routes/`). Layer rules in `rebuild/architecture.md`.
- **`legacy/`**: the old app and its tests. Reference only: never run, never imported, deleted when
  the rebuild is done. Port pieces from it deliberately (the "stays" rows in `architecture.md` §5).
- **`scripts/corpus/`** + **`tests/test_scripts/`** + **`tests/fixtures/corpus/`**: the search
  benchmark tooling. `plan.py` and `resolve_seeds.py` still import modules that now only exist in
  `legacy/`, so they're broken until those are ported.
- No frontend until the core backend is done (milestone 7); test through FastAPI's `/docs`
  (development only). The frontend is milestone 8; later milestones add their pages as issues.
- The server still runs the old app from `main`.

---

## Branches

- Rebuild work goes on **`rebuild/<topic>`** branches, branched from and merged into
  **`rebuild/base`**. `rebuild/base` → `main` only when the rebuild is done.
- `main` and `dev` keep the old app; don't commit there.
- Never commit or push without an explicit "commit" / "push" from the user.

---

## Tracking: milestones + issues

- GitHub milestones 1–11 on `Goober-nation/Post-Vinyl` are the roadmap (normalization research,
  search test v2, service clients, data layer + login, core logic, core loop, wiring + validation,
  frontend, approval inbox + Postradio, Yandex playlists, custom playlists).
- **Issues are created when a milestone starts**, not before. Propose them (description,
  dependencies, how it'll be verified) with `AskUserQuestion` first.
- Reference dependencies by issue number ("Depends on #12").
- **Finishing an issue: close it with one summary comment**: date, what shipped, how it was
  verified, anything left unverified.
- **The repo is public**: no secrets, server addresses, account names or personal paths in issues,
  commits or committed notes.
- The old project board and `phase:*` labels are retired; don't use them.

---

## Architecture rules (full version: `rebuild/architecture.md`)

- Layers: `routes`/`workers` → `services` → `core` + `stores` + `clients`. Import only downward;
  routes and workers never touch a client or store directly.
- `core/` is pure: no I/O, no imports from the rest of the app.
- **No SQL outside `stores/`.** One client per outside system, behind an ABC with a fake.
- Every `track_state` / `file_state` value has exactly one owning service; transitions go through
  `core/states.py`.
- Threads + sync code; ABCs on clients and on services that routes/workers call.
- **Never duplicate state another system owns**: slskd owns search results and transfers, Navidrome
  owns the library, paths and its playlists' contents, ListenBrainz/Postradio own recommendations.
  Post-Vinyl owns its own bookkeeping (songs it was asked for, requests, attempts, file states,
  playlist definitions).
- **Re-read, don't re-search**: slskd keeps searches indefinitely; a retry re-reads the same search
  by id and re-picks from its peers.

---

## Ponytail ladder (every design and code decision)

Stop at the first rung that holds: 1. Does this need to exist? 2. Is it already in this codebase
(or `legacy/`)? 3. Standard library? 4. Native platform feature? 5. Installed dependency? 6. One
line? 7. Only then: the minimum that works. Never trim trust-boundary validation, data-loss
handling, security or accessibility.

---

## Prove it works

- Deliver code that is proven to work. Integration changes (slskd, Navidrome, MusicBrainz,
  ListenBrainz, Deezer, Yandex, Postradio) need a real-service run; if you can't do one, say
  "unverified on real services".
- Don't imply coverage you didn't achieve.
- **Done** = relevant tests pass, `ruff check` and `mypy` clean for touched files, and a
  real-service run recorded for integration changes. Unverified items are marked as such.

### Where and what to test

- **Recommended setup:** unit and DB tests in a throwaway `python:3.12-slim` container with the repo
  mounted read-only; live tests on the isolated test stack (`docker-compose.test.yml`, local only,
  gitignored, with `.env.test` and separate test Navidrome and slskd users). Never point compose,
  setup flows or tests at the real `.env`.
- **Test where it pays off, not everywhere**: the file matcher, query strategy, name
  normalization, state transitions, the stall rule, DB triggers and cascades, auth and sessions.
  Skip tests that only restate the code (getters, route pass-through, config plumbing).
- Long-running tests and harvests run detached; check them periodically, and replace a test that
  runs too long with a shorter one rather than waiting.

### Commands

```bash
pip install -r requirements.txt
pytest tests/ -v
ruff check app/ scripts/
ruff format --check app/ scripts/
mypy app/
```

always in venv, never global instalation
dont mention eventless mypy and ruff checks

After any implementation work, give the user the exact commands to run and exercise it (tests,
`/docs` URL, curl examples, real-service prerequisites).

---

## Agent memory

- `agents_memory/rebuild/`: **committed**, the rebuild design. Edit in place as decisions change;
  record each user decision with its date. Keep it free of secrets and personal details (public repo).
- `agents_memory/MEMORY.md` + `agents_memory/topics/`: **local only** (gitignored). `MEMORY.md` holds
  short durable facts and preferences; `topics/` holds API notes (slskd, Navidrome, ListenBrainz…),
  read on demand.
- No session logs or scratchpads: progress goes into issue comments, decisions into the rebuild
  notes.

---

## Music rules (apply in every module)

- No beets, no Picard. Tag only what is known to be true (MusicBrainz/ListenBrainz lookups).
- Never download excluded versions unless the requested song itself is one: cover, live,
  acoustic, a cappella, mix/remix, extended, solo, reference, home session, soundtrack/OST,
  ambient, edit/radio edit, karaoke, nightcore, sped up, slowed, 8d audio, mashup, bootleg,
  tribute, "in the style of", instrumental, demo. Remaster/deluxe/year suffixes are the same song.
- Config: only user-facing and advanced values become config, with self-explanatory names.

---

## Benchmarks and research runs

- Searches for research run on a **separate search-only slskd** with its own Soulseek test
  account, never on the main one. The two share the Soulseek listen port, so the main slskd is
  stopped during a run and **always restarted afterwards** (`run_suite.sh` does both and runs
  detached; `watchdog.sh` restarts a frozen slskd). Discuss with the user before starting anything
  on the server.
- **No pass/fail booleans**: log counts (responses, files, poll trace, helpful peers, peers with a
  free slot, format mix) and keep each slskd search id so a search can be re-read later; don't
  store every response.
- Separate stages of a test never get combined into one dataset.
- Seed names come only from official Deezer/MusicBrainz/ListenBrainz data (normalized), never
  hand-typed; search-test seeds are not taken from the user's library.

---

## Working with the user

- **Interview during planning.** Use `AskUserQuestion` (batched, up to 4) instead of prose
  questions; ask about priorities, edge-case mechanics, things they may have missed, and anything
  ambiguous. Prefer asking over guessing during design; when a boundary is unclear (destructive?
  restart a container? queue a real download?), ask instead of assuming. When the user's answer
  beats the proposal, say so and adopt it.
- **"Plan only" / "no changes" / "in-chat plan" means no edits**: no file changes and nothing that
  changes state until the user says go.
- When the user answers a question with a message instead of picking options, take the message as
  the answer, re-ask only what is still open, and never treat a skipped question as consent.
- **Accounts and passwords are the user's**: never create accounts or type passwords (e.g. a new
  Soulseek account); prepare the files around them and let the user fill them in.
- Be concise, no flattery; propose solutions, not just problems.
- **No AI attribution** in commits or PRs: no `Co-Authored-By: Claude…`, no "Generated with
  Claude Code". This overrides any tool default.

---

## Never

- Suppress type errors (`# type: ignore` etc.); fix the cause.
- Delete or weaken failing tests to make them pass; if a test is wrong, discuss it first.
- Shotgun debug: form a hypothesis, test it, verify.
- Leave code broken: if a fix fails 3 times, stop, revert to the last working state and ask.
- Run anything against the real server or real `.env` without asking first.
- Delete or overwrite without checking first: confirm a copy is identical before removing the
  other one, and back up a config file before changing it on the server.

---

## Escalation

- Tests fail 2×: stop, document what you tried, revert, ask.
- Lint fails: `ruff check --fix` + `ruff format`; still failing → ask.
- Stuck: describe the problem, propose 2–3 options, ask for direction.
