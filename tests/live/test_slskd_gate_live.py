"""slskd gate on the test slskd (#100). About 80 s: 5 searches + 1 check.

set -a; . ./.env.test; set +a; PV_LIVE=1 pytest tests/live/test_slskd_gate_live.py -v -s
"""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Callable

import pytest

from app.clients.slskd import SearchState, SlskdClient
from app.config import SlskdSettings
from app.services.slskd_gate import ParentLost, SlskdGate

pytestmark = pytest.mark.skipif(
    os.environ.get("PV_LIVE") != "1", reason="set PV_LIVE=1 and source .env.test"
)
SLSKD_URL = os.environ.get("PV_TEST_SLSKD_URL", "http://127.0.0.1:8191")


def make_run(client: SlskdClient) -> Callable[[str, Callable[[], bool]], SearchState]:
    """A plain search: start, poll every 2 s until slskd ends it (#101 adds the
    stable-count exit)."""

    def run(query: str, still_ready: Callable[[], bool]) -> SearchState:
        search_id = client.start_search(query, response_limit=600, idle_timeout_s=8)
        while not (state := client.get_search(search_id)).is_complete:
            if not still_ready():
                client.cancel_search(search_id)
                raise ParentLost
            time.sleep(2)
        print(f"  {query!r}: {state.response_count} responses, id {search_id}")
        return state

    return run


def test_gate_on_real_slskd() -> None:
    client = SlskdClient(SLSKD_URL, os.environ["SLSKD_API_KEY"])
    gate = SlskdGate(client, SlskdSettings())
    assert gate.ready(), "test slskd must be logged in with a network parent"
    run = make_run(client)

    starts = [
        gate.search(q, run, lambda s: s.response_count).started_at
        for q in ("nujabes feather", "aphex twin xtal", "burial archangel",
                  "boards of canada roygbiv", "madvillain accordion")
    ]  # fmt: skip
    gaps = [round(b - a, 1) for a, b in itertools.pairwise(starts)]
    print("start gaps (s):", gaps)
    assert starts[4] - starts[0] >= 60  # the 5th start waited for the window

    # nothing came back with results "from now on", so the real check runs
    trusted = gate.empty_trusted(time.monotonic() + 1, run, lambda s: s.response_count)
    print("check trusted:", trusted)
    assert trusted, "the '2020' check should pass on a healthy, unthrottled account"
