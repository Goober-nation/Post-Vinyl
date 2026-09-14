# AGENTS.md — Post-Vinyl AI Guide

**Purpose**: Instant context for AI agents working on this project. Read this first.

---

## Rule Zero: Prove It Works

**Your job is to deliver code that is proven to work.**

- Every PR must pass tests locally before opening
- slskd/Navidrome integration changes must be tested with real services
- If you cannot test on real services, say so explicitly: "unverified on real services, waiting for user testing"
- Do not imply coverage you did not actually achieve

---

## Build & Test Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run app
python -m app.main

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=app --cov-report=term-missing

# Lint
ruff check app/
ruff format --check app/

# Type check
mypy app/
```

---

## Definition of Done

Before marking any item as `[x]` (AI-tested):

- [ ] All tests pass (`pytest` exit code 0)
- [ ] Linting passes (`ruff check` exit code 0)
- [ ] No type errors (`mypy` exit code 0)
- [ ] Documentation updated if public API changed
- [ ] Tested with real slskd/Navidrome if integration changed

---

## Multi-Phase Testing

This project uses a 3-phase completion model, tracked via GitHub Issues + the project board's Status
field (Todo/In Progress/Done) rather than bracket codes in a markdown file (that system was retired
2026-09-15 — see "GitHub Backlog & Project Board" below):

1. **AI-tested**: You implemented, ran tests, verified functionality — leave an issue comment
   summarizing what was verified (date + what, in ~1 sentence), and set board Status to reflect it.
2. **User-tested**: User deployed in Docker, confirmed real-world functionality — a follow-up comment.
3. **Complete**: User-tested + no issues + approved — close the issue.

**Your workflow:**
- Start work: move the board item's Status to "In Progress" (or just start — Status is a courtesy for
  visibility, not a hard gate)
- Finish work: leave an issue comment "AI-tested: YYYY-MM-DD — [what was verified, ~1 sentence]"; do
  not restate the whole implementation — that belongs in the code and, if it's a lasting design
  decision, in `agents_memory/topics/*.md`
- User approves: close the issue with a closing comment "User-tested: YYYY-MM-DD — [feedback]"

---

## GitHub Backlog & Project Board

**Location**: GitHub Issues on this repo + the [Post-Vinyl Backlog](https://github.com/users/Goober-nation/projects/1)
project board (public). The board's **Phase** field groups issues into the roadmap order (numbered,
lowest = next); its **Status** field (Todo/In Progress/Done) tracks in-flight work state independent
of phase. Phase labels (`phase:stability-pass`, `phase:search-pipeline`, etc.) mirror the board field
so phase membership is visible directly on the issue list too, not just on the board.

The local `backlog/` directory's live-planning files (`BACKLOG.md`, `phase-*.md`, `parked.md`) were
retired 2026-09-15 once every still-relevant item was ported to GitHub issues — do not recreate them.
A `backlog/` directory may still exist locally on this machine with pre-migration historical archives
in it (gitignored, never pushed, not part of this repo's history) — it is not part of any workflow,
is never read at session start, and this file makes no assumption it exists. Do not rely on it or
point anyone else at it; the GitHub issues/board above are the only shared source of truth.

**Session startup:**
1. Check the project board (group by Phase) or `gh issue list --label phase:<current-phase>` for the
   active phase's open items.
2. Read the issue(s) you're about to work — including comments, which carry prior test plans,
   decisions, and cross-references to related issues (see e.g. #10↔#34, #6↔#35, #8↔#33 for the
   pattern).
3. If the user gives a free-form note/idea mid-session, propose which existing issue it belongs to
   (or that it needs a new one) before acting — same spirit as the old "process user notes" step, just
   via `AskUserQuestion` instead of a markdown notes section.

**Adding new items:**
- Propose the new item (description, dependencies, testing plan) via `AskUserQuestion` before creating
  it, per "Interview the User During Planning" below
- `gh issue create` with a real description; label it (`bug`/`enhancement` + the relevant `phase:*`
  label); add it to the project board (`gh project item-add 1 --owner Goober-nation --url <issue-url>`)
  and set its Phase field to match
- Reference real dependencies by issue number in the body ("Depends on #39"), not a made-up ID scheme

**Finishing a phase:**
- Close each issue as it completes (see Multi-Phase Testing above) — GitHub tracks this natively, no
  manual archiving step needed. A phase is "done" when every issue carrying its label is closed.

**Never:**
- Reorder phases (the board's Phase field) without user approval
- Close an issue without a summary comment of what shipped/was verified
- Recreate `backlog/BACKLOG.md` or a new local planning file — GitHub is the source of truth now

---

## Agent Memory System

**Location**: `agents_memory/`

**Purpose**: Store durable, cross-session facts that aren't already derivable from the code or from the GitHub issues/project board. AGENTS.md never changes; `agents_memory/` captures dynamic knowledge — and stays small on purpose.

**Structure:**
```
agents_memory/
├── README.md              # Explains the memory system
├── MEMORY.md              # Long-term curated facts (~50 lines max, enforced)
└── topics/
    ├── slskd-api.md       # Topic-specific notes, read on-demand
    └── navidrome-integration.md
```

**No `SCRATCHPAD.md` and no `daily/` log.** They were dropped 2026-08-09: both duplicated the backlog (which already tracks "what's next" and "what happened") and grew without bound — a single-day `daily/` file reached 791 lines and was mandatory reading every session. GitHub issues/comments are now the one place status lives (see "GitHub Backlog & Project Board" above); `MEMORY.md` is the one place durable facts live. Do not recreate either file — if you're tempted to write "here's what happened this session," that content belongs in an issue comment (if it's about task progress) or `MEMORY.md` (if it's a durable fact/gotcha), not a new log.

**Decision guide — What to write where:**

| What happened | Write to | Why |
|---|---|---|
| Finished/updated a backlog item | An issue comment | That's the log — one line, not a narrative |
| Discovered an API quirk worth more than a line | `topics/<domain>.md` | Reusable knowledge, read on-demand |
| User preference ("always do X") | `MEMORY.md` | Durable fact, needs to be in every session |
| Critical gotcha that would bite almost any future session | `MEMORY.md` | High-signal, keep it under the line cap |
| Narrow gotcha specific to one module/endpoint | `topics/<domain>.md` | Not worth the MEMORY.md line budget |

**When to write to MEMORY.md:**
- User states a lasting preference
- A decision becomes permanent
- A gotcha that isn't narrow to one file/endpoint

**When NOT to write to MEMORY.md:**
- Information already derivable from the code itself
- Anything narrow enough for `topics/` instead
- Session narration — there is no daily log anymore; don't recreate one inside MEMORY.md either

**MEMORY.md format:**
```markdown
# Long-Term Memory

## Architecture Decisions
- slskd 0.26.0 requires cancel-to-flush pattern (2026-08-08)
- State stored in SQLite, not JSON files (2026-08-08)

## User Preferences
- User prefers ABC interfaces over Protocol classes (2026-08-08)
- User wants hot-reload for config, secrets require restart (2026-08-08)

## Known Gotchas
- slskd search responses return [] until search completes (2026-08-08)
- Navidrome playlist API uses songIdToAdd, not songId (2026-08-08)

## Project Status
- One line per phase — current state only, not how it got there
```

**topics/ format:**
```markdown
# slskd API

## Search
- POST /api/v0/searches → search_id
- GET /api/v0/searches/{id} → metadata (responseCount, isComplete)
- PUT /api/v0/searches/{id} → cancel (forces response flush)
- GET /api/v0/searches/{id}/responses → peer responses

## Gotchas
- Responses return [] until search completes or is cancelled
- retention.search config controls how long searches persist (default: indefinite)
- response_file_limit: 100 per peer

## Examples
- Cancel-to-flush pattern: see app/services/search.py:drive_search()
```

---

## Session Startup Checklist

**Every session, in order:**

1. Read `AGENTS.md` (this file)
2. Read `agents_memory/MEMORY.md` (long-term facts — should be a quick read)
3. Check the project board / relevant `phase:*` label for open items; read the specific issue(s)
   (including comments) for the task at hand — this checklist has no local-file step; everything
   needed to pick up work lives on GitHub
4. If the user gives a free-form note/idea, propose which issue it belongs to (or that it needs a new
   one) before acting
5. Identify next task from the board
6. Begin work
7. Read a specific `topics/*.md` file only if the task actually touches that area — not mandatory reading every session

**If anything is unclear or missing, ask the user before proceeding.**

---

## Anti-Patterns (Never Do These)

❌ **Never suppress type errors**
- No `as any`, `@ts-ignore`, `@ts-expect-error`
- Fix the root cause

❌ **Never write state files directly**
- Always use `state_manager.load()` / `save()`
- Never write to `data/*.json` directly

❌ **Never fetch slskd responses without cancel**
- Use `drive_search()` which handles cancel-to-flush
- Responses return `[]` mid-search

❌ **Never start a *new* search on retry** (reworded 2026-08-11)
- Re-read the **same** search by `search_id` and re-pick from its peers, skipping tried/blocked/bad ones
- The old wording said "reuse stored `search_responses` from state" — a leftover from when retry
  genuinely re-searched, and later misread as "musica must keep its own copy of the results".
  It must not: **slskd owns search results and retains them** (`retention.search`, default
  indefinite; verified 2026-08-11 that they survive a slskd restart). Migration 005 dropped the
  duplicate copy. Re-fetching via `fetch_search_responses(search_id)` is not a fresh search —
  no new `POST /api/v0/searches`, same completed search, same candidate pool.

❌ **Never duplicate state another service already owns**
- Before persisting anything, ask which service is the system of record. slskd owns search
  results and transfer state; Navidrome owns the library and playlists; ListenBrainz owns
  recommendations. musica owns its *own* bookkeeping — search headers, download rows, worker
  state — and nothing else
- A local copy costs more than disk: `SlskdSearch._hydrate()` briefly loaded every stored
  response on startup, making it proportional to all history

❌ **Never commit without explicit request**
- User must say "commit" or "push"

❌ **Never delete failing tests to "pass"**
- Fix the root cause
- If test is wrong, discuss with user first

❌ **Never shotgun debug**
- Don't make random changes hoping something works
- Form hypothesis, test it, verify

❌ **Never leave code in broken state**
- If fix fails 3x, stop and ask user
- Revert to last known working state

---

## Key Files

| File | Purpose |
|------|---------|
| `AGENTS.md` | This file (AI guide, never changes) |
| [GitHub Project board](https://github.com/users/Goober-nation/projects/1) | Phase + Status tracking, read every session (replaces the old `backlog/BACKLOG.md`) |
| GitHub Issues (this repo) | One issue per work item; comments carry test plans/decisions — read the ones for the active phase |
| `agents_memory/MEMORY.md` | Durable cross-session facts, ~50 lines (read every session) |
| `agents_memory/topics/` | Deep domain-specific notes (read on-demand) |
| `app/` | Python backend source code |
| `tests/` | Test suite |
| `config.toml` | Application configuration |
| `README.md` | User-facing overview, quick start, FAQ, acknowledgments |
| `docs/architecture.md` | How services/workers/DB fit together, recs mechanics, import pipeline |
| `docs/api.md` | Full HTTP endpoint reference |
| `docs/deployment.md` | Docker Compose setup, proxy/VPN, Tailscale, troubleshooting |

---

## Escalation Rules

**If tests fail 3x:**
- Stop and ask user
- Document what you tried
- Revert to last known working state

**If linting fails:**
- Run `ruff check --fix` and `ruff format`
- If still failing, ask user

**If build fails:**
- Check error message
- Fix, retry once
- If still failing, ask user

**If you're stuck:**
- Document the problem
- Propose 2-3 possible solutions
- Ask user for direction

---

## Communication Style

- Be concise, no flattery
- Start work immediately, no status updates
- Ask clarifying questions when scope is ambiguous
- Propose solutions, not just problems
- Use todos for multi-step tasks

---

## Interview the User During Planning (2026-08-10)

**The user explicitly wants to be interviewed, not just presented with a plan.** During any
design/planning/backlog session, actively ask questions rather than filling gaps with
assumptions — the user has stated they "can't always think of these things" and wants the AI
to surface the decisions they haven't considered.

- **Use the native `AskUserQuestion` tool** for these, not prose questions buried in a wall of
  text. The user asked for this specifically. Batch related questions (up to 4 per call).
- Ask about: **priorities** (what goes in which phase, what's MVP vs. later), **mechanics**
  (how exactly a feature should behave in edge cases), **things they may have missed**, and
  **anything ambiguous** in their own notes.
- When their answer reveals a better idea than what was proposed, say so and adopt it — the
  user's domain instincts have repeatedly been right (e.g. pooling Deep Cuts instead of a
  per-playlist count).
- Prefer asking over guessing even when a reasonable default exists, *during planning*. This
  overrides the general "make the reasonable call and keep going" bias — that still applies to
  implementation work, not to design sessions.
- **When in doubt, ask more, not fewer questions** (user, 2026-08-11): *"better safe than sorry
  for sure… I am 100% for more questions before you start, to have 0 ambiguity remaining, I
  prefer that."* Do not economize on clarifying questions to seem decisive, and do not narrow
  your own permissions out of caution — if a boundary is unclear (is this action destructive?
  am I allowed to restart a container? may I queue a real download?), **ask instead of
  silently assuming the conservative reading**. An unasked question that leads to half-done
  work is worse than one more round of `AskUserQuestion`.
- Answers to these interviews are decisions: record them as a comment on the relevant GitHub issue
  (and `agents_memory/topics/*.md` if they're lasting architectural choices) so they don't get
  re-litigated next session.

---

## Give the User Run Commands (2026-08-08)

**The user runs the code themselves.** After any implementation work, always provide the exact commands they need to run it:

- How to start the app (`python -m app.main` or `uvicorn app.main:app --port 8000`)
- How to run tests / lint / type check (`pytest tests/ -v`, `ruff check app/`, `mypy app/`)
- How to exercise what was just built (curl examples, URL to open in browser)
- Note real-service prerequisites (e.g., "requires slskd reachable at the configured URL")

Include these in the final answer of every work session, not just when asked. If the user runs the code and reports a problem, fix it and re-verify — do not assume their environment matches yours.

---

**End of AGENTS.md**
