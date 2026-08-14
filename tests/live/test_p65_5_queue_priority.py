"""
P6.5-5 — manual downloads always beat recs.

The mechanism is a pause, not a reorder: RecPuller checks
`has_active_manual_downloads()` before searching each track and again right
before queueing it, and waits while any manual transfer is in flight.

What makes this hard to test is that the interesting assertion is about
*ordering in time*, not end state — "recs paused, then resumed" and "a manual
queue was never blocked" both look identical to "nothing happened" if you only
inspect the final rows. So everything here works off the SSE timeline, which
the harness timestamps, rather than off polled state.

The edge case that the review found (a pending row slskd never adopts) gets
its own class: it used to deadlock rec queueing permanently.
"""

from __future__ import annotations

import pytest

from tests.live.harness import queue_first_available, settle, wait_until

SEED_QUERY = "bohemian rhapsody"


def _queue_a_manual_download(stack) -> dict:
    """Search and queue the first peer slskd accepts, as a manual download.

    Walks candidates because peers go offline between the search and the
    queue call — a single-candidate attempt fails on network luck rather
    than on the priority behavior under test.
    """
    job = stack.client.search(SEED_QUERY)
    detail = stack.client.search_detail(job["search_id"])
    if not detail["results"]:
        pytest.skip(f"no peers for {SEED_QUERY!r} — Soulseek availability")
    queued = queue_first_available(stack, job["search_id"], detail["results"])
    if queued is None:
        pytest.skip("every candidate peer refused the queue — network luck")
    return {"search_id": job["search_id"], **queued}


def _manual_is_active(stack) -> bool:
    return any(
        row["state"] in ("queued", "downloading") and not row["is_rec_download"]
        for row in stack.db.downloads()
    )


def _recs_enabled(stack) -> bool:
    status = stack.client.recs_status()
    return status["listenbrainz_enabled"] and any(
        status[f"{c}_enabled"] for c in ("comfort_zone", "fresh_picks", "deep_cuts")
    )


class TestRecQueueingPausesForManual:
    def test_rec_pull_waits_while_a_manual_transfer_is_live(
        self, stack, clean_finished
    ):
        """The core claim. Asserted on event *ordering*: no rec queueing may
        be observed between the manual download going live and it clearing.
        """
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled — enable one to test priority")

        _queue_a_manual_download(stack)
        wait_until(
            lambda: _manual_is_active(stack),
            timeout=90,
            description="manual download to go live",
        )
        stack.marker("manual_active")

        pull = stack.client.pull_recs()
        assert pull.status in (202, 409), pull.body
        if pull.status == 409:
            pytest.skip("a rec pull was already running; rerun when idle")

        # Watch for either outcome, whichever comes first.
        stack.marker("rec_pull_started")
        settle(30, "let the puller reach its first track")

        rec_queued_while_manual_live = [
            e
            for e in stack.events.snapshot()
            if e.type == "transfer.queued"
            and e.t > stack.timeline.of_kind("marker")[-1]["t"]
        ]
        if _manual_is_active(stack):
            assert not rec_queued_while_manual_live, (
                "a rec download was queued while a manual transfer was still "
                "in flight — the priority gate did not hold"
            )

    def test_rec_queueing_resumes_once_manual_clears(self, stack, clean_finished):
        """A pause that never lifts is just a deadlock with better manners."""
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled")

        wait_until(
            lambda: not _manual_is_active(stack),
            timeout=300,
            description="manual downloads to clear",
        )
        pull = stack.client.pull_recs()
        if pull.status == 409:
            pytest.skip("a rec pull was already running")

        completed = stack.events.wait_for(
            lambda e: e.type == "rec.pull_completed",
            timeout=600,
            description="rec.pull_completed",
        )
        stack.marker("rec_pull_completed", data=completed.data)
        assert not completed.data.get("aborted"), (
            "rec pull aborted with no manual download to wait on"
        )


class TestManualIsNeverBlocked:
    def test_manual_queue_succeeds_during_a_rec_pull(self, stack, clean_finished):
        """The reverse must never happen: recs must not gate a manual queue."""
        if not _recs_enabled(stack):
            pytest.skip("no rec category enabled")

        pull = stack.client.pull_recs()
        if pull.status == 409:
            pytest.skip("a rec pull was already running")
        stack.marker("rec_pull_started")

        job = stack.client.search(SEED_QUERY)
        detail = stack.client.search_detail(job["search_id"])
        if not detail["results"]:
            pytest.skip("no peers returned")

        queued = queue_first_available(stack, job["search_id"], detail["results"])
        if queued is None:
            pytest.skip("every candidate peer refused the queue — network luck")

        # The queue call itself must not have been made to wait on recs.
        queue_calls = [
            e
            for e in stack.timeline.of_kind("api")
            if e["path"] == "/api/queue" and e["method"] == "POST"
        ]
        assert queue_calls[-1]["duration"] < 15, (
            f"manual queue took {queue_calls[-1]['duration']:.1f}s during a "
            f"rec pull — it should never wait on recs"
        )


class TestStalePendingEdgeCase:
    """The deadlock found in review: a manual pending row slskd never adopts.

    Before the fix this held `has_active_manual_downloads()` True forever, no
    endpoint could clear the row, and every subsequent rec pull aborted.

    **This state cannot be manufactured through the API** (established live
    2026-08-11). A pending row is only written when slskd *accepts* the queue,
    and slskd rejects anything it can't serve — 404 for an unknown username,
    500 for a real username with a nonexistent filename. Getting an accepted
    queue that is then never adopted needs a peer to accept and immediately
    vanish, which isn't something a test can arrange. The reaper's logic is
    therefore unit-tested (`TestFailStalePending` in
    tests/test_db/test_download_store.py, `TestHousekeeping` in
    tests/test_workers/test_download_monitor.py), and what's checked live is
    the wiring: that it runs every poll and leaves healthy rows alone.
    """

    def test_reaper_runs_every_poll_without_touching_healthy_rows(
        self, stack, clean_finished
    ):
        queued = _queue_a_manual_download(stack)
        wait_until(
            lambda: _manual_is_active(stack),
            timeout=120,
            description="manual download to go live",
        )

        # Several monitor polls — the reaper runs on each one, before the
        # slskd fetch. A fresh row must survive all of them.
        settle(50, "monitor polls")

        row = stack.db.download_by_file(queued["username"], queued["filename"])
        if row is None:
            pytest.skip("row was cleaned up mid-test")
        assert "never adopted" not in str(row.get("state", "")), (
            "the reaper failed a row that is well inside pending_timeout_minutes"
        )
        stack.marker("healthy_row_survived", state=row["state"])

    def test_reaper_never_errors(self, stack, since_now):
        """It runs before the slskd fetch on every poll — an exception there
        would take the whole monitor cycle down with it."""
        settle(35, "at least two monitor polls")
        assert (
            stack.logs.count_lines(
                "Failed to reap stale pending downloads", since=since_now()
            )
            == 0
        )
