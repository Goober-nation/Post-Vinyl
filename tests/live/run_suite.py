#!/usr/bin/env python3
"""
The one serialized driver for a full live pipeline run.

Everything else in `tests/live/` can be run by hand; this is what produces
the statistics. It exists because a real run has constraints a bare `pytest`
invocation cannot honour:

- **One run at a time.** There is one slskd, one music tree, one database.
  Two overlapping runs would produce garbage and blame each other for it.
  Held by a lock file for the whole run.
- **A hard download ceiling.** Real transfers are the scarce resource. The
  budget is file-backed and shared across every pytest invocation below, so
  the ceiling survives the several processes this script starts.
- **The user's config must come back.** The run enables all three rec
  categories; `config/config.toml` is snapshotted byte-for-byte up front and
  restored in a `finally`, so an interrupted run does not leave the user's
  settings rewritten.
- **Partial results must survive.** Each scenario group is its own pytest
  process. One that dies — or kills the musica container, which U7 does on
  purpose — does not take the rest of the run with it, and the scorecard
  already holds everything learned up to that point.

Usage:

    python3 tests/live/run_suite.py                  # 3 reps, 40 downloads
    python3 tests/live/run_suite.py --reps 1 --budget 12
    python3 tests/live/run_suite.py --only U1_manual_pipeline
    python3 tests/live/run_suite.py --report-only live-artifacts/<run>/

Expect 2-3 hours for the default. Nothing needs watching: results are written
incrementally, and `report.py` runs at the end (and can be re-run any time
against the artifact directory).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.live import report as report_mod
from tests.live.harness import REPO_ROOT, MusicaClient, diagnose_unreachable
from tests.live.scenarios import DEFAULT_DOWNLOAD_BUDGET

CONFIG_PATH = REPO_ROOT / "config" / "config.toml"
LOCK_PATH = REPO_ROOT / "live-artifacts" / ".run_suite.lock"

#: Scenario groups, in execution order, each its own pytest process.
#:
#: U1 runs first and against the whole corpus — it is the journey the user
#: actually described, and its per-tier numbers carry most of the report.
#: The resilience scenarios follow. U7 is deliberately **last**: it SIGKILLs
#: the musica container, and a crash test that poisons every later result
#: would be measuring itself.
GROUPS: tuple[tuple[str, str], ...] = (
    ("U1_manual", "test_scenarios.py::test_u1_manual_pipeline"),
    ("stages_import", "test_stages_import.py"),
    ("stages_library", "test_stages_library.py"),
    ("U5_duplicate", "test_scenarios.py::test_u5_duplicate_download"),
    ("U8_stale_row", "test_scenarios.py::test_u8_stale_beets_row"),
    ("U6_peer_retry", "test_scenarios.py::test_u6_peer_failure_retry"),
    ("U2_deep_cuts", "test_scenarios.py::test_u2_deep_cuts"),
    ("U3_comfort_zone", "test_scenarios.py::test_u3_comfort_zone"),
    ("U4_fresh_picks", "test_scenarios.py::test_u4_fresh_picks"),
    ("U9_playlist", "test_scenarios.py::test_u9_playlist_lifecycle"),
    ("U10_concurrent", "test_scenarios.py::test_u10_concurrent_downloads"),
    ("U7_crash", "test_scenarios.py::test_u7_crash_recovery"),
)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def exclusive_run():
    """Refuse to start alongside another run.

    A stale lock from a killed process is reported rather than silently
    stolen — if two runs really did overlap, every number in both is suspect
    and the user needs to know that, not have it papered over.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            held = json.loads(LOCK_PATH.read_text())
        except (OSError, ValueError):
            held = {}
        raise SystemExit(
            f"another live run holds the lock ({held.get('started', '?')}, "
            f"pid {held.get('pid', '?')}, artifacts {held.get('artifacts', '?')}).\n"
            f"If that process is dead, delete {LOCK_PATH} and try again."
        )
    # Local time on purpose: this string is read by whoever finds a stale
    # lock, and UTC would just make them do arithmetic.
    started = datetime.now().isoformat()  # noqa: DTZ005
    LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "started": started}))
    try:
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


@contextlib.contextmanager
def preserved_config():
    """Snapshot config.toml and put it back, whatever happens.

    The run enables rec categories the user has switched off. Restoring them
    is not optional and cannot depend on the run finishing cleanly.
    """
    original = CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else None
    try:
        yield
    finally:
        if original is not None and CONFIG_PATH.read_bytes() != original:
            CONFIG_PATH.write_bytes(original)
            print("[suite] restored config/config.toml to its original contents")


def enable_all_rec_categories(musica_url: str) -> None:
    """Turn on all three categories for the run, via the app's own API.

    Through the API rather than by editing the file, so musica's hot-reload
    sees it the same way a user toggling the switches would.
    """
    client = MusicaClient(musica_url)
    call = client.set_config(
        "recs",
        comfort_zone_enabled=True,
        fresh_picks_enabled=True,
        deep_cuts_enabled=True,
    )
    if call.status not in (200, 201, 204):
        print(f"[suite] WARNING: could not enable rec categories ({call.status})")
    else:
        print("[suite] enabled comfort_zone + fresh_picks + deep_cuts for this run")


def require_stack(musica_url: str) -> None:
    if not MusicaClient(musica_url).is_up():
        raise SystemExit(
            f"musica did not answer at {musica_url}.\n"
            f"{diagnose_unreachable(musica_url)}\n"
            f"  If the stack is simply down: docker compose up -d"
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_group(
    label: str,
    target: str,
    *,
    artifacts: Path,
    run_id: str,
    budget: int,
    musica_url: str,
    extra: list[str],
) -> int:
    """One pytest process. Its exit code is informational, not fatal.

    A non-zero exit here does NOT mean the pipeline is broken — the scenarios
    record findings rather than raising. It means the harness hit something,
    which is worth printing but never worth aborting the rest of the run for.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        f"tests/live/{target}",
        "--live",
        f"--artifacts={artifacts}",
        f"--download-budget={budget}",
        f"--musica-url={musica_url}",
        "-q",
        "--no-header",
        "-p",
        "no:randomly",
        *extra,
    ]
    env = {**os.environ, "MUSICA_LIVE_RUN_ID": run_id}
    started = time.monotonic()
    print(f"\n[suite] === {run_id} / {label} ===", flush=True)
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    print(
        f"[suite] {label} finished in {time.monotonic() - started:.0f}s "
        f"(exit {proc.returncode})",
        flush=True,
    )
    return proc.returncode


def do_reset(artifacts: Path, musica_url: str, db_path: Path) -> None:
    """Back up and wipe, once, before the repetitions start.

    Deliberately not per-repetition: the dedup and placement questions are
    about what accumulates across a realistic session, and resetting between
    reps would hide exactly the drift the user is complaining about.
    """
    from tests.live.probes.reset import full_reset

    print("[suite] full reset (backing up first)...", flush=True)
    result = full_reset(artifacts / "backup", musica_url=musica_url, db_path=db_path)
    print(
        f"[suite] backed up {len(result.backed_up)} item(s), wiped "
        f"{len(result.wiped)}, forgot {result.slskd_transfers_forgotten} slskd "
        f"transfer(s) ({result.slskd_transfers_forget_failed} failed), musica "
        f"down {result.downtime_s}s",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive a full live pipeline run.")
    parser.add_argument("--reps", type=int, default=3, help="repetitions (default 3)")
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_DOWNLOAD_BUDGET,
        help=f"hard ceiling on REAL downloads (default {DEFAULT_DOWNLOAD_BUDGET})",
    )
    parser.add_argument("--artifacts", type=Path, default=None)
    parser.add_argument("--musica-url", default="http://localhost:8092")
    parser.add_argument(
        "--db-path", type=Path, default=REPO_ROOT / "app_data" / "musica.db"
    )
    parser.add_argument(
        "--only", action="append", default=[], help="run only these groups"
    )
    parser.add_argument("--skip-reset", action="store_true")
    parser.add_argument("--report-only", type=Path, default=None)
    parser.add_argument("pytest_args", nargs="*", help="extra args passed to pytest")
    args = parser.parse_args(argv)

    if args.report_only:
        report_mod.generate(args.report_only)
        print(
            report_mod.render_markdown(
                report_mod.build_summary(
                    report_mod.Scorecard.load(args.report_only / "scorecard.jsonl")
                )
            )
        )
        return 0

    artifacts = args.artifacts or (
        REPO_ROOT / "live-artifacts" / datetime.now().strftime("run-%Y%m%d-%H%M%S")  # noqa: DTZ005
    )
    artifacts.mkdir(parents=True, exist_ok=True)

    groups = [g for g in GROUPS if not args.only or g[0] in args.only]
    if not groups:
        raise SystemExit(f"no groups matched --only {args.only}")

    require_stack(args.musica_url)

    print(f"[suite] artifacts: {artifacts}")
    print(
        f"[suite] {args.reps} rep(s), {len(groups)} group(s), budget {args.budget} downloads"
    )

    with exclusive_run(), preserved_config():
        if not args.skip_reset:
            do_reset(artifacts, args.musica_url, args.db_path)
        enable_all_rec_categories(args.musica_url)

        started = time.monotonic()
        for rep in range(1, args.reps + 1):
            run_id = f"rep{rep}"
            for label, target in groups:
                try:
                    run_group(
                        label,
                        target,
                        artifacts=artifacts,
                        run_id=run_id,
                        budget=args.budget,
                        musica_url=args.musica_url,
                        extra=list(args.pytest_args),
                    )
                except KeyboardInterrupt:
                    print("\n[suite] interrupted — writing the report for what ran")
                    report_mod.generate(artifacts)
                    return 130
            # Regenerate after every repetition, so an abandoned run still
            # leaves a readable report rather than only raw JSONL.
            report_mod.generate(artifacts)
            print(
                f"[suite] rep{rep} done, report refreshed at {artifacts / 'report.md'}"
            )

        elapsed = time.monotonic() - started

    summary = report_mod.generate(artifacts)
    totals = summary["totals"]
    print(f"\n[suite] complete in {elapsed / 60:.0f} min")
    print(
        f"[suite] {totals['graded_stages']} graded stages, "
        f"{totals['journeys']} journeys, outcomes: {totals['outcomes']}"
    )
    print(f"[suite] report:  {artifacts / 'report.md'}")
    print(f"[suite] summary: {artifacts / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
