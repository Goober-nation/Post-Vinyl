"""slskd gate: readiness, rate cap, lost parent, trusting empty results (#100)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from app.clients.slskd import AppState, SlskdError
from app.clients.slskd_fake import FakeSlskdClient
from app.config import ConfigError, SlskdSettings, load_slskd
from app.services.slskd_gate import ParentLost, SlskdGate

SETTINGS = SlskdSettings()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class Slskd(FakeSlskdClient):
    """app_state() answers from a script, then stays at the last value."""

    def __init__(self, states: list[AppState | Exception] | None = None) -> None:
        super().__init__()
        self.script = states or []

    def app_state(self) -> AppState:
        if self.script:
            s = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(s, Exception):
                raise s
            return s
        return self.state


READY = AppState(True, True)
NO_PARENT = AppState(True, False)
LOGGED_OUT = AppState(False, False)


def answers(*counts: int) -> tuple[Callable[[str, Callable[[], bool]], int], list[str]]:
    """A search that returns the given response counts in turn; logs queries."""
    queue, seen = list(counts), []

    def run(query: str, still_ready: Callable[[], bool]) -> int:
        seen.append(query)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return run, seen


def ident(n: int) -> int:
    return n


def gate(
    slskd: Slskd | None = None, clock: Clock | None = None
) -> tuple[SlskdGate, Clock]:
    clock = clock or Clock()
    return SlskdGate(slskd or Slskd(), SETTINGS, clock, clock.sleep), clock


# ------------------------------------------------------------ rate cap


def test_at_most_four_starts_in_any_minute() -> None:
    g, _ = gate()
    run, _ = answers(10)
    starts = [g.search("q", run, ident).started_at for _ in range(9)]
    for i in range(len(starts) - 4):
        assert starts[i + 4] - starts[i] >= 60
    assert starts[:4] == [1000.0] * 4  # no wait while under the cap


# ------------------------------------------------------------ readiness


def test_waits_without_limit_until_ready() -> None:
    slskd = Slskd([LOGGED_OUT] * 30 + [NO_PARENT] * 30 + [READY])
    g, clock = gate(slskd)
    run, seen = answers(5)
    ran = g.search("q", run, ident)
    assert seen == ["q"] and ran.started_at == 1000.0 + 60 * 5  # 60 not-ready checks
    assert set(clock.slept) == {5}


def test_slskd_unreachable_counts_as_not_ready() -> None:
    g, _ = gate(Slskd([SlskdError("down"), READY]))
    run, seen = answers(5)
    g.search("q", run, ident)
    assert seen == ["q"]


def test_parent_lost_during_search_runs_it_again() -> None:
    g, _ = gate()
    calls: list[int] = []

    def run(query: str, still_ready: Callable[[], bool]) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise ParentLost
        return 7

    assert g.search("q", run, ident).result == 7 and len(calls) == 2


def test_not_ready_right_after_search_runs_it_again() -> None:
    # ready to start, not ready after the first run, then ready for good
    g, _ = gate(Slskd([READY, NO_PARENT, READY]))
    run, seen = answers(0, 9)
    assert g.search("q", run, ident).result == 9 and seen == ["q", "q"]


def test_search_gets_a_live_readiness_check() -> None:
    slskd = Slskd()
    g, _ = gate(slskd)
    seen: list[bool] = []

    def run(query: str, still_ready: Callable[[], bool]) -> int:
        seen.append(still_ready())
        slskd.state = NO_PARENT
        seen.append(still_ready())
        slskd.state = READY
        return 1

    g.search("q", run, ident)
    assert seen == [True, False]


# ------------------------------------------------------------ trusting empty results


def test_empty_is_trusted_when_a_later_search_had_enough_results() -> None:
    g, clock = gate()
    run, seen = answers(0, 12, 450)
    empty = g.search("a", run, ident)
    clock.t += 1
    g.search(
        "b", run, ident
    )  # 12 responses: a throttled account can still return a few
    clock.t += 1
    g.search("c", run, ident)  # 450 ≥ 300: proof of health
    assert g.empty_trusted(empty.started_at, run, ident)
    assert seen == ["a", "b", "c"]  # no check search needed


def test_a_few_responses_do_not_prove_health() -> None:
    g, _ = gate()
    run, seen = answers(0, 12, 600)
    since = g.search("a", run, ident).started_at
    g.search("b", run, ident)
    assert g.empty_trusted(since, run, ident)
    assert seen == ["a", "b", "2020"]  # 12 wasn't enough: the check ran


def test_untrusted_empty_runs_the_check_and_passes() -> None:
    g, _ = gate()
    run, seen = answers(0, 0, 600)
    since = g.search("a", run, ident).started_at
    g.search("b", run, ident)
    assert g.empty_trusted(since, run, ident)
    assert seen == ["a", "b", "2020"]
    assert g.empty_trusted(since, run, ident)  # a passed check counts as results
    assert seen[-1] == "2020" and len(seen) == 3


def test_failed_check_pauses_searching() -> None:
    g, clock = gate()
    run, seen = answers(0, 36, 5)  # throttled: the check gets 36 < 300
    since = g.search("a", run, ident).started_at
    assert not g.empty_trusted(since, run, ident)
    paused_at = clock.t
    g.search("a", run, ident)  # the caller's redo waits out the pause
    assert clock.t >= paused_at + 10 * 60
    assert seen == ["a", "2020", "a"]


def test_low_count_search_does_not_prove_health_for_the_check() -> None:
    """A throttled check with some responses must not mark slskd healthy."""
    g, _ = gate()
    run, seen = answers(0, 36, 600)
    since = g.search("a", run, ident).started_at
    assert not g.empty_trusted(since, run, ident)  # check got 36
    assert g.empty_trusted(since, run, ident)  # so it checks again: 600
    assert seen == ["a", "2020", "2020"]


def test_one_search_at_a_time() -> None:
    g, _ = gate(clock=None)
    inside, overlap = [0], [False]
    lock = threading.Lock()

    def run(query: str, still_ready: Callable[[], bool]) -> int:
        with lock:
            inside[0] += 1
            overlap[0] |= inside[0] > 1
        threading.Event().wait(0.02)
        with lock:
            inside[0] -= 1
        return 1

    threads = [
        threading.Thread(target=g.search, args=("q", run, ident)) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not overlap[0]


# ------------------------------------------------------------ config


def test_example_config_has_the_defaults() -> None:
    assert load_slskd(Path("config/post-vinyl.example.yaml")) == SlskdSettings()


def test_missing_section_means_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("sources: {}\n")
    assert load_slskd(p) == SlskdSettings()


@pytest.mark.parametrize(
    "text",
    [
        "slskd:\n  searches_per_minute: 0\n",
        "slskd:\n  searches_per_minute: '4'\n",
        "slskd:\n  searches_per_minute: true\n",
        "slskd:\n  throttle_check_query: ''\n",
        "slskd:\n  speed: 1\n",
        "slskd: [1]\n",
    ],
)
def test_bad_slskd_values_refuse(tmp_path: Path, text: str) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(text)
    with pytest.raises(ConfigError):
        load_slskd(p)


def test_no_search_starts_while_the_queue_is_paused() -> None:
    clock = Clock()
    paused = [True, True, True, False]  # resumed after three checks
    gate = SlskdGate(Slskd(), SETTINGS, clock, clock.sleep,
                     paused=lambda: paused.pop(0) if len(paused) > 1 else paused[0])  # fmt: skip
    run, seen = answers(400)
    gate.search("q", run, lambda n: n)
    assert seen == ["q"] and clock.slept[:3] == [5, 5, 5]
