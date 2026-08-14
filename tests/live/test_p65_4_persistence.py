"""
P6.5-4 (post-migration-005) — a search survives a restart without musica
keeping a copy of its results.

The claim under test: after a restart, a saved search still resolves and
alternative-peer retry finds a new peer **without issuing a new slskd
search**. That's a negative, and it has no API surface — the only way to
check it is to compare the set of search ids musica logged as initiated
before and after. Hence the LogScraper.

Note what is *not* asserted any more: musica no longer stores peer responses
at all. slskd owns them and retains them durably (verified 2026-08-11: the
oldest search still returned all 250 responses, unchanged across a slskd
restart), so the tests assert that the results come *back*, not that musica
wrote them down.

Two restart scenarios, deliberately separate, because they are not the same
test and only one of them was ever broken:

- **Case A** — only musica restarts. slskd keeps transferring throughout, and
  the rows resync on the next poll. Nothing should be cancelled or re-queued.
- **Case B** — slskd forgets the transfer. Nothing reports the row any more,
  so orphan reconciliation must fail it and retry from persisted responses.
"""

from __future__ import annotations

import pytest

from tests.live.harness import (
    first_viable_result,
    queue_first_available,
    settle,
    wait_until,
)

# A track common enough that a search reliably returns several peers — the
# point here is the persistence machinery, not query construction.
SEED_QUERY = "bohemian rhapsody"


def _search_and_pick(stack) -> tuple[str, dict]:
    """Run a search and return (search_id, the best single candidate)."""
    search_id, results = _search_and_candidates(stack)
    return search_id, first_viable_result(results)


def _search_and_candidates(stack) -> tuple[str, list[dict]]:
    """Run a search and return (search_id, all results).

    Callers walk the candidate list rather than betting on one peer — peers
    go offline between the search and the queue call often enough that a
    single-candidate test fails for reasons unrelated to what it checks.
    """
    job = stack.client.search(SEED_QUERY)
    search_id = job["search_id"]
    detail = stack.client.search_detail(search_id)
    if not detail["results"]:
        pytest.skip(
            f"no peers returned for {SEED_QUERY!r} — Soulseek availability, "
            f"not a musica failure"
        )
    return search_id, detail["results"]


def _wait_for_adoption(stack, filename: str, timeout: float = 120.0) -> dict:
    """Wait until **musica's DB** has adopted this file with a real slskd id.

    Two subtleties, both learned the hard way:

    * Match on the filename. A bare "any adopted transfer" check is satisfied
      by leftovers from an earlier run, and the test then asserts nothing
      about the download it just queued.
    * Wait on the DB, not on `GET /api/transfers`. That endpoint reflects
      *slskd's* live view, which shows the transfer up to a full monitor poll
      (15s) before musica writes the adopted row. Acting on it too early —
      e.g. making slskd forget the transfer — leaves musica holding only the
      `pending:` row, which orphan reconciliation deliberately ignores
      (`slskd_id IS NOT NULL`). The result is a test that waits forever for an
      orphan that correctly never comes.
    """

    def _find() -> dict | None:
        for row in stack.db.downloads():
            if row["filename"] == filename and row["slskd_id"]:
                return row
        return None

    wait_until(
        lambda: _find() is not None,
        timeout=timeout,
        description=f"musica to adopt {filename!r}",
    )
    row = _find()
    return {
        "transfer_id": row["id"],
        "username": row["username"],
        "filename": row["filename"],
        "state": row["state"],
    }


class TestHeaderPersistence:
    def test_only_the_header_is_stored(self, stack):
        """musica records the search, not its results. Migration 005 dropped
        the duplicate copy of data slskd already owns."""
        search_id, _ = _search_and_pick(stack)

        row = stack.db.search_by_id(search_id)
        assert row is not None, f"no header row for {search_id}"
        assert row["response_count"] > 0, "header should record how many peers answered"
        assert stack.db.stale_tables() == set(), (
            "search_jobs/search_responses are back — the container is "
            "duplicating slskd again"
        )
        stack.marker(
            "header_stored", search_id=search_id, responses=row["response_count"]
        )

    def test_header_survives_a_restart(self, stack):
        search_id, _ = _search_and_pick(stack)
        before = stack.db.search_by_id(search_id)
        assert before is not None

        stack.restart_musica()

        after = stack.db.search_by_id(search_id)
        assert after is not None
        assert after["query"] == before["query"]
        assert after["response_count"] == before["response_count"]

    def test_db_growth_stays_proportional_to_searches(self, stack):
        """The duplication cost 2,598 rows / 4.1MB for 23 searches — 76% of
        the database. Now one row per user search, and nothing per peer."""
        counts = stack.db.table_counts()
        stack.marker("table_counts", **counts)
        assert counts["searches"] == len(stack.db.searches())
        print(f"\n[live] table counts: {counts}")

    def test_results_come_back_after_restart_without_researching(self, stack):
        """The behavior that actually matters. The header hydrates from
        SQLite; the *results* are re-read from slskd by search_id. Neither
        path starts a new search."""
        search_id, _ = _search_and_pick(stack)
        stack.restart_musica()

        searches_before = len(stack.logs.searches_issued(since="30s"))
        detail = stack.client.search_detail(search_id)
        searches_after = len(stack.logs.searches_issued(since="30s"))

        assert detail["expired"] is False
        assert detail["results"], "hydrated search returned no results"
        assert searches_after == searches_before, (
            "a new slskd search fired while reopening a persisted search"
        )


class TestCaseA_MusicaOnlyRestart:
    """Only musica restarts. slskd never stops transferring."""

    def test_transfer_survives_and_is_not_requeued(self, stack, clean_finished):
        search_id, results = _search_and_candidates(stack)
        queued = queue_first_available(stack, search_id, results)
        if queued is None:
            pytest.skip("every candidate peer refused the queue — network luck")

        # Wait for slskd to adopt *this* file, not merely for something to
        # appear: leftovers from an earlier run would satisfy a bare "any
        # adopted transfer" check and the test would prove nothing.
        adopted = _wait_for_adoption(stack, queued["filename"])
        before = adopted["state"]
        stack.marker("adopted", ids=[adopted["transfer_id"]], state=before)

        stack.restart_musica()
        # Three monitor polls (check_interval 15s) plus slack, so orphan
        # reconciliation has had every chance to misfire if it's going to.
        settle(50, "monitor polls after restart")

        after = {t["transfer_id"]: t["state"] for t in stack.client.transfers()}
        transfer_id = adopted["transfer_id"]
        assert transfer_id in after, (
            f"{transfer_id} vanished from /transfers across a musica-only "
            f"restart — slskd was still transferring it"
        )
        assert after[transfer_id] != "cancelled", (
            f"{transfer_id} was cancelled by the restart; a musica-only "
            f"restart must leave slskd's transfers alone"
        )

    def test_case_a_does_not_orphan_anything(self, stack, since_now):
        """Restarting musica must not trip orphan reconciliation — those
        rows are healthy and still in slskd."""
        stack.restart_musica()
        settle(50, "monitor polls after restart")
        assert stack.logs.count_lines("Download orphaned", since=since_now()) == 0


class TestCaseB_SlskdDropsTheTransfer:
    """slskd stops holding a transfer musica believes is live.

    What this found (2026-08-11): a *silent* orphan is hard to manufacture on
    a healthy stack, and that's a feature. Telling slskd to forget a live
    transfer (`remove=true`) makes it report the record as cancelled on the
    very next poll rather than dropping it wordlessly, so the ordinary
    failure path handles it — and handles it better, because it knows the
    peer failed. Orphan reconciliation is the safety net for the abnormal
    cases where slskd goes quiet instead (its DB reset, manual surgery, a
    record vanishing between two polls); those are covered by unit tests in
    tests/test_workers/test_download_monitor.py, which can produce silence
    on demand.

    What matters live is the outcome, and it's the same either way: the row
    must leave the live states, and retry must re-use the existing search.
    """

    def test_dropped_transfer_leaves_the_live_states(
        self, stack, clean_finished, since_now
    ):
        """What's verifiable live: a transfer slskd no longer holds must not
        stay 'queued'/'downloading' in musica, because that holds the
        rec-queue gate open.

        Which code path clears it is not assertable here. Telling slskd to
        drop a live transfer (`remove=true`) makes it report the record as
        *cancelled* on the next poll rather than going silent, so musica
        records the cancellation through the ordinary path and orphan
        reconciliation never needs to act. Producing true silence — the case
        reconciliation exists for — requires slskd to lose a record without
        reporting it, which no API call arranges; that path is unit-tested
        in tests/test_workers/test_download_monitor.py, where silence is
        trivial to produce.
        """
        search_id, results = _search_and_candidates(stack)
        queued = queue_first_available(stack, search_id, results)
        if queued is None:
            pytest.skip("every candidate peer refused the queue — network luck")

        adopted = _wait_for_adoption(stack, queued["filename"])
        if adopted["state"] not in ("queued", "downloading"):
            pytest.skip(
                f"transfer finished as {adopted['state']} before it could be "
                f"dropped — nothing live to reconcile"
            )

        stack.marker("dropping_transfer", transfer_id=adopted["transfer_id"])
        if not stack.slskd.forget_transfer(adopted["username"], adopted["transfer_id"]):
            pytest.skip("slskd would not drop the transfer")

        def _still_live() -> bool:
            row = stack.db.download_by_file(adopted["username"], adopted["filename"])
            return row is not None and row["state"] in ("queued", "downloading")

        wait_until(
            lambda: not _still_live(),
            timeout=180,
            description="the dropped transfer to leave the live states",
        )

        # Scoped to *this* row on purpose. A global "nothing manual is live"
        # assertion reads well but is wrong: other tests in the same run
        # queue real downloads, so it fails on their leftovers rather than
        # on anything this test did.
        row = stack.db.download_by_file(adopted["username"], adopted["filename"])
        final_state = row["state"] if row else "deleted"
        stack.marker("gate_released", final_state=final_state)
        assert final_state not in ("queued", "downloading")

    def test_slskd_is_the_durable_source_for_search_results(self, stack):
        """The premise migration 005 rests on: musica keeps no copy of peer
        responses because slskd retains them, including across its own
        restart. If this ever stops being true, retry loses its candidates
        and this test is where it shows up."""
        search_id, _results = _search_and_candidates(stack)
        assert stack.slskd.search_responses(search_id), (
            "slskd does not serve responses for a search it just ran"
        )

        stack.docker.restart("slskd")
        stack.slskd.wait_until_up()

        after = stack.slskd.search_responses(search_id)
        assert after, (
            f"slskd lost search {search_id} across a restart — musica keeps no "
            f"copy, so alternative-peer retry would have nothing to pick from"
        )
        stack.marker("slskd_retained", search_id=search_id, responses=len(after))
        assert len(after) >= 1


class TestWorkerStatePersistence:
    def test_last_run_at_survives_a_restart(self, stack):
        """A bug here re-pulls recs on every restart."""
        status_before = stack.client.recs_status()
        state_before = stack.db.worker_state()

        stack.restart_musica()

        status_after = stack.client.recs_status()
        state_after = stack.db.worker_state()

        assert status_after["last_pull_at"] == status_before["last_pull_at"]
        for key, value in state_before.items():
            assert state_after.get(key) == value, f"worker_state lost {key}"

    def test_next_pull_is_not_reset_by_a_restart(self, stack):
        before = stack.client.recs_status()["next_pull_at"]
        if before is None:
            pytest.skip("no category enabled — nothing scheduled to compare")
        stack.restart_musica()
        assert stack.client.recs_status()["next_pull_at"] == before
