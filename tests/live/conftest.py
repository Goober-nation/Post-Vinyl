"""
Live-test fixtures and opt-in gating.

These tests drive the real Docker stack: they queue real downloads from real
Soulseek peers, restart containers, and read the live database. `pytest tests/`
must stay safe to run on any machine, so everything here is skipped unless
`--live` is passed explicitly.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.live.harness import (
    DEFAULT_DB_PATH,
    DEFAULT_MUSICA_URL,
    DbInspector,
    MusicaClient,
    build_stack,
    diagnose_unreachable,
)
from tests.live.probes import Probes, Scorecard
from tests.live.probes.reset import ResetReport
from tests.live.probes.reset import full_reset as run_full_reset
from tests.live.scenarios import (
    DEFAULT_DOWNLOAD_BUDGET,
    DownloadBudget,
    ScenarioContext,
    current_run_id,
)


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live-stack tests against the running Docker containers.",
    )
    parser.addoption(
        "--musica-url",
        action="store",
        default=DEFAULT_MUSICA_URL,
        help=f"Base URL for musica (default: {DEFAULT_MUSICA_URL}).",
    )
    parser.addoption(
        "--db-path",
        action="store",
        default=str(DEFAULT_DB_PATH),
        help="Host path to musica.db (default: ./app_data/musica.db).",
    )
    parser.addoption(
        "--artifacts",
        action="store",
        default="",
        help="Directory for per-test timeline JSONL. Default: a temp dir.",
    )
    parser.addoption(
        "--download-budget",
        action="store",
        type=int,
        default=DEFAULT_DOWNLOAD_BUDGET,
        help=(
            "Hard ceiling on REAL downloads for this run "
            f"(default: {DEFAULT_DOWNLOAD_BUDGET}). Shared across every pytest "
            "invocation that points at the same --artifacts directory."
        ),
    )


#: This directory. Membership is tested by path, not by substring.
_LIVE_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    """Skip everything under `tests/live/` unless `--live` was passed.

    Matched by directory, deliberately. The check used to be
    `"live" in str(item.fspath)`, which also caught every *unit* test whose
    filename happens to contain "live" — `tests/test_live_harness.py` and
    `tests/test_live_report.py` are both pure, stack-free tests of parsing
    and aggregation logic, and both were being silently skipped in any full
    `pytest tests/` run while passing when run directly. That is the worst
    shape a test bug can take: the logic that makes every live assertion
    meaningful was itself unverified, and the run still looked green.
    """
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live-stack test; pass --live to run")
    for item in items:
        path = Path(str(item.fspath)).resolve()
        if path.is_relative_to(_LIVE_DIR):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def musica_url(request) -> str:
    return request.config.getoption("--musica-url")


@pytest.fixture(scope="session")
def db_path(request) -> Path:
    return Path(request.config.getoption("--db-path"))


@pytest.fixture(scope="session")
def artifact_root(request, tmp_path_factory) -> Path:
    configured = request.config.getoption("--artifacts")
    # Resolved to an absolute path: a relative --artifacts is otherwise
    # interpreted against whatever CWD the run happens to have, and the
    # teardown line prints a relative path that gives no hint where the
    # file actually landed.
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else tmp_path_factory.mktemp("live")
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session", autouse=True)
def _require_stack(request, musica_url, db_path):
    """Fail fast and loudly if the stack isn't actually up.

    Without this a live run reports a wall of confusing connection errors
    instead of the one fact that matters.
    """
    if not request.config.getoption("--live"):
        return
    client = MusicaClient(musica_url)
    # Wait, don't probe once. `is_up()` allows a single 5s response, which a
    # *healthy* musica routinely exceeds while it is importing through beets
    # or servicing a search flush. A one-shot probe here turned that ordinary
    # slowness into `pytest.exit`, aborting the whole group before a single
    # test ran: in the 2026-08-12 run it silently killed 13 groups — every
    # rec-category scenario in reps 1 and 2, and U9 entirely — while musica
    # was up and healthy the whole time. Losing a third of a three-hour run
    # to a health check that was wrong is far worse than waiting a minute for
    # a stack that really is down.
    try:
        client.wait_until_up(timeout=90.0)
    except TimeoutError:
        # Do not name musica as the culprit before checking whether it is.
        # On 2026-08-13 this message aborted four runs while musica was
        # answering inside its container in 13ms throughout; what had
        # actually stalled was the host-side port forwarder, saturated by
        # slskd's Soulseek peer connections. `diagnose_unreachable` probes
        # the vantages that tell those apart.
        diagnosis = diagnose_unreachable(musica_url)
        pytest.exit(
            f"musica did not answer at {musica_url} for 90s.\n{diagnosis}\n"
            f"  If the stack is simply down: docker compose up -d",
            returncode=1,
        )
    if not db_path.exists():
        pytest.exit(f"musica.db not found at {db_path}", returncode=1)

    # The container can be running happily on an out-of-date image, in which
    # case assertions fail with a bare "no such table" that reads like a
    # harness bug. Say the actual thing instead.
    inspector = DbInspector(db_path)
    missing = inspector.missing_tables()
    if missing:
        pytest.exit(
            f"musica.db is missing {sorted(missing)} — the running container "
            f"is out of date. Rebuild first: docker compose up -d --build musica",
            returncode=1,
        )
    stale = inspector.stale_tables()
    if stale:
        pytest.exit(
            f"musica.db still has {sorted(stale)} — the running container "
            f"predates migration 005, so it is still duplicating slskd's "
            f"search results. Rebuild first: docker compose up -d --build musica",
            returncode=1,
        )


@pytest.fixture
def stack(request, musica_url, db_path, artifact_root):
    """A wired live Stack, with a per-test timeline artifact."""
    test_dir = artifact_root / request.node.name
    test_dir.mkdir(parents=True, exist_ok=True)
    gen = build_stack(musica_url=musica_url, db_path=db_path, artifact_dir=test_dir)
    stack = next(gen)
    stack.marker("test_start", test=request.node.name)
    try:
        yield stack
    finally:
        stack.marker("test_end", test=request.node.name)
        for _ in gen:  # drive the generator's finally block
            pass
        print(f"\n[live] timeline: {test_dir / 'timeline.jsonl'}")


@pytest.fixture
def clean_finished(stack):
    """Clear finished transfers before and after, so state-count assertions
    aren't polluted by earlier runs."""
    stack.client.delete_finished()
    yield
    stack.client.delete_finished()


@pytest.fixture(scope="session")
def scorecard(artifact_root) -> Scorecard:
    """The one append-only record of every graded stage in the run.

    Session-scoped and written incrementally: a suite that dies at hour two
    still leaves everything it learned in the first two hours at
    `<artifacts>/scorecard.jsonl`.
    """
    card = Scorecard(artifact_root / "scorecard.jsonl")
    yield card
    print(f"\n[live] scorecard: {card.path} ({len(card.results)} graded stages)")


@pytest.fixture(scope="session")
def probes() -> Probes:
    """Every measurement instrument: `.navidrome .fs .tags .beets .lb`.

    Session-scoped because each one is a stateless client; construction
    does no I/O, so this is free for a run that ends up skipping.
    """
    return Probes()


@pytest.fixture
def full_reset(request, musica_url, db_path, artifact_root):
    """Back up, wipe, and restart onto a clean pipeline. **Destructive.**

    Deletes `app_data/musica.db`, both beets profile databases, and the
    contents of the `Searches`, `Discovery` and `downloads` trees — after
    copying all of them to `<artifacts>/backup/`. The backup is not
    optional: nothing is deleted unless the copy landed first.

    Double-guarded. `--live` is checked here, and the collection hook
    already skips every test under `tests/live/` without it, so this cannot
    fire on an ordinary `pytest tests/` run.

    Yields a `ResetReport` — what was backed up, what was wiped, how long
    musica was down, and the tables migrations recreated.
    """
    if not request.config.getoption("--live"):
        pytest.skip("full_reset is destructive; pass --live to run it")

    backup_dir = artifact_root / "backup"
    report: ResetReport = run_full_reset(
        backup_dir, musica_url=musica_url, db_path=db_path
    )
    print(
        f"\n[live] full reset: backed up {len(report.backed_up)} item(s) to "
        f"{backup_dir}, wiped {len(report.wiped)}, musica down "
        f"{report.downtime_s}s"
    )
    yield report


@pytest.fixture(scope="session")
def budget(request, artifact_root) -> DownloadBudget:
    """The run's hard ceiling on real downloads.

    File-backed under `<artifacts>/`, because `run_suite.py` invokes pytest
    several times and a per-process counter would silently reset on each
    one — spending the user's whole download budget several times over.
    """
    return DownloadBudget(
        total=request.config.getoption("--download-budget"),
        path=artifact_root / "download_budget.json",
    )


@pytest.fixture
def scenario_ctx(stack, probes, scorecard, budget, artifact_root) -> ScenarioContext:
    """Everything a full-pipeline journey in `scenarios.py` needs.

    `run_id` comes from `MUSICA_LIVE_RUN_ID` when the runner set it, so all
    the pytest invocations belonging to one repetition group together in the
    report; an ad-hoc `pytest --live` gets its own.
    """
    return ScenarioContext(
        stack=stack,
        probes=probes,
        scorecard=scorecard,
        run_id=current_run_id(),
        budget=budget,
        artifact_dir=artifact_root,
    )


@pytest.fixture
def since_now() -> str:
    """A `docker logs --since` timestamp for 'from this moment on'.

    Docker takes RFC3339 or a Go duration; a duration avoids clock-skew
    between the host and the container entirely.
    """
    start = time.monotonic()

    def _since() -> str:
        elapsed = int(time.monotonic() - start) + 2
        return f"{elapsed}s"

    return _since
