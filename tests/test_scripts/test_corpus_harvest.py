"""Tests for the standalone harvester against a fake slskd (#34, #74)."""

import gzip
import json

import pytest

from scripts.corpus import harvest


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSlskd:
    """Scripted slskd: `counts` is responseCount per poll; completes on cancel."""

    def __init__(
        self, counts, responses, complete_at=None, empty_fetches=0, files=None
    ):
        self.counts = list(counts)
        self.files = list(files) if files is not None else [c * 2 for c in counts]
        self.resp = responses
        self.complete_at = complete_at
        self.empty_fetches = empty_fetches
        self.cancelled = False
        self.polls = 0
        self.started = []

    def server_state(self):
        return {"isConnected": True, "isLoggedIn": True}

    def version(self):
        return {"version": "test", "distributedNetwork": {"hasParent": True}}

    def start(self, query, response_limit, timeout_ms):
        self.started.append((query, response_limit))
        return "sid"

    def meta(self, _sid):
        i = min(self.polls, len(self.counts) - 1)
        self.polls += 1
        done = self.cancelled or (
            self.complete_at is not None and i >= self.complete_at
        )
        return {
            "responseCount": self.counts[i],
            "fileCount": self.files[min(i, len(self.files) - 1)],
            "isComplete": done,
        }

    def cancel(self, _sid):
        self.cancelled = True

    def responses(self, _sid):
        if self.empty_fetches:
            self.empty_fetches -= 1
            return []
        return self.resp


def _run(fake, **kw):
    clock = FakeClock()
    return harvest.run_search(
        fake,
        "q",
        response_limit=kw.get("limit", 600),
        clock=clock,
        sleep=clock.sleep,
    )


def test_natural_completion_is_not_cancelled():
    fake = FakeSlskd([0, 3, 5], [{"username": "a"}] * 5, complete_at=2)
    r = _run(fake)
    assert r["stop_reason"] == "complete" and not fake.cancelled
    assert [t[1] for t in r["trace"]] == [0, 3, 5]


def test_stops_when_file_count_is_stable_for_8s():
    # files grow until the 3rd poll (4 s), then stay: stop at 4 + 8 = 12 s.
    fake = FakeSlskd([1, 2, 3] + [3] * 50, [{"username": "a"}] * 3)
    r = _run(fake)
    assert r["stop_reason"] == "stable" and fake.cancelled
    assert r["trace"][-2][0] == 12


def test_never_stops_before_5s_even_when_nothing_comes():
    fake = FakeSlskd([0] * 50, [])
    r = _run(fake)
    assert r["stop_reason"] == "stable"
    assert r["trace"][-2][0] == 8  # unchanged from 0 s; stable window, not min, decides


def test_polls_every_2s():
    fake = FakeSlskd([1, 2, 3] + [3] * 50, [{"username": "a"}] * 3)
    times = [t[0] for t in _run(fake)["trace"]]
    assert times[:4] == [0, 2, 4, 6]


def test_hard_timeout_at_30s_while_files_keep_coming():
    fake = FakeSlskd([1] * 100, [{"username": "a"}], files=range(100))
    r = _run(fake)
    assert r["stop_reason"] == "timeout"
    assert r["trace"][-2][0] == 30


def test_response_cap_cancels():
    fake = FakeSlskd([10, 600], [{"username": "a"}] * 600)
    r = _run(fake)
    assert r["stop_reason"] == "response_cap" and fake.cancelled
    assert not r["response_count_mismatch"]


def test_slskd_ending_at_the_cap_counts_as_response_cap():
    class Capped(FakeSlskd):
        def meta(self, sid):
            return super().meta(sid) | {"state": "Completed, ResponseLimitReached"}

    r = _run(Capped([600], [{"username": "a"}] * 600, complete_at=0))
    assert r["stop_reason"] == "response_cap"


def test_flush_lag_is_waited_out():
    fake = FakeSlskd([4], [{"username": "a"}] * 4, complete_at=0, empty_fetches=2)
    r = _run(fake)
    assert len(r["responses"]) == 4


def test_short_response_list_is_recorded_not_failed():
    fake = FakeSlskd([4], [{"username": "a"}] * 3, complete_at=0)
    r = _run(fake)
    assert r["response_count_mismatch"] and not r["flush_failed"]


def test_empty_list_with_responses_is_flush_failure():
    fake = FakeSlskd([4], [], complete_at=0)
    assert _run(fake)["flush_failed"]


def test_peer_summary_and_counts():
    resp = [
        {
            "username": "p",
            "hasFreeUploadSlot": True,
            "queueLength": 3,
            "uploadSpeed": 120000,
            "files": [{"filename": "a.flac"}, {"filename": "b.flac"}],
            "lockedFiles": [{"filename": "c.flac"}],
        }
    ]
    r = _run(FakeSlskd([1], resp, complete_at=0))
    assert r["response_count"] == 1 and r["file_count"] == 2
    assert r["peers"] == [
        {
            "username": "p",
            "free_slot": True,
            "queue_length": 3,
            "upload_speed": 120000,
            "files": 2,
            "locked_files": 1,
        }
    ]


def test_wait_logged_in_raises_when_never_connected():
    class Down(FakeSlskd):
        def server_state(self):
            return {"isConnected": False}

    clock = FakeClock()
    with pytest.raises(RuntimeError, match="not logged in"):
        harvest.wait_logged_in(Down([0], []), max_wait=0, sleep=clock.sleep)


def test_main_records_everything_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest.time, "sleep", lambda s: None)
    plan = {
        "seeds": [{"id": "s1", "type": "artist", "artist": "Air"}],
        "queries": [
            {"qid": "q1", "query": "Air", "uses": [{"seed": "s1", "shape": "A"}]},
            {"qid": "q2", "query": "Air band", "uses": [{"seed": "s1", "shape": "X"}]},
        ],
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    out = tmp_path / "out"
    resp = [{"username": "p", "files": [{"filename": "Air\\Talkie Walkie\\01.flac"}]}]
    (out / "raw").mkdir(parents=True)
    harvest.save(out / "raw" / "q2.json.gz", {"done": True})

    fake = FakeSlskd([1], resp, complete_at=0)
    argv = [
        "--plan",
        str(tmp_path / "plan.json"),
        "--out",
        str(out),
        "--per-minute",
        "6000",
    ]
    assert harvest.main(argv, slskd=fake) == 0

    # warm-up first, then q1; q2 already on disk → skipped
    assert fake.started == [("love", 600), ("Air", 600)]
    with gzip.open(out / "raw" / "q1.json.gz", "rt") as fh:
        rec = json.load(fh)
    assert rec["query"] == "Air" and rec["uses"] == [{"seed": "s1", "shape": "A"}]
    assert rec["responses"] == resp and rec["stop_reason"] == "complete"
    assert rec["meta"]["responseCount"] == 1 and rec["trace"]
    run = json.loads((out / "runs.jsonl").read_text().splitlines()[0])
    assert run["response_limit"] == 600 and run["slskd_version"]["version"] == "test"
    assert (run["poll"], run["stable"], run["min_wait"], run["max_wait"]) == (
        2,
        8,
        5,
        30,
    )
    assert json.loads((out / "seeds.json").read_text())[0]["artist"] == "Air"


def test_result_discarded_when_connection_drops_during_search(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest.time, "sleep", lambda s: None)
    plan = {"seeds": [], "queries": [{"qid": "q1", "query": "first", "uses": []}]}
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    class Drops(FakeSlskd):
        calls = 0

        def server_state(self):
            # logged in before each search, gone right after it
            self.calls += 1
            return {
                "isConnected": self.calls % 2 == 1,
                "isLoggedIn": self.calls % 2 == 1,
            }

    fake = Drops([1], [{"username": "a"}], complete_at=0)
    argv = [
        "--plan",
        str(tmp_path / "plan.json"),
        "--out",
        str(tmp_path / "o"),
        "--per-minute",
        "6000",
        "--retries",
        "0",
    ]
    harvest.main(argv, slskd=fake)
    assert not (tmp_path / "o" / "raw" / "q1.json.gz").exists()


def test_throttled_search_backs_off_and_retries(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr(harvest.time, "sleep", waits.append)
    plan = {"seeds": [], "queries": [{"qid": "q1", "query": "x", "uses": []}]}
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    class Busy(FakeSlskd):
        tries = 0

        def start(self, query, response_limit, timeout_ms):
            if query == "x":
                self.tries += 1
                if self.tries == 1:
                    raise harvest.Throttled("start: HTTP 429")
            return super().start(query, response_limit, timeout_ms)

    fake = Busy([1], [{"username": "a"}], complete_at=0)
    argv = ["--plan", str(tmp_path / "plan.json"), "--out", str(tmp_path / "o")]
    harvest.main(argv, slskd=fake)
    assert 30 in waits
    assert (tmp_path / "o" / "raw" / "q1.json.gz").exists()


def test_crash_mid_run_keeps_finished_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest.time, "sleep", lambda s: None)
    queries = [{"qid": f"q{i}", "query": f"x{i}", "uses": []} for i in range(3)]
    (tmp_path / "plan.json").write_text(json.dumps({"seeds": [], "queries": queries}))
    argv = ["--plan", str(tmp_path / "plan.json"), "--out", str(tmp_path / "o")]

    class Crashes(FakeSlskd):
        def start(self, query, response_limit, timeout_ms):
            if query == "x1":
                raise KeyboardInterrupt
            return super().start(query, response_limit, timeout_ms)

    with pytest.raises(KeyboardInterrupt):
        harvest.main(argv, slskd=Crashes([1], [{"username": "a"}], complete_at=0))
    raw = tmp_path / "o" / "raw"
    assert sorted(p.name for p in raw.iterdir()) == ["q0.json.gz"]  # no partial files

    fake = FakeSlskd([1], [{"username": "a"}], complete_at=0)
    harvest.main(argv, slskd=fake)
    # resumes where it stopped (after its own warm-up search)
    assert [q for q, _ in fake.started] == ["love", "x1", "x2"]


def test_warm_up_retries_until_a_search_answers():
    class Cold(FakeSlskd):
        searches = 0

        def start(self, query, response_limit, timeout_ms):
            self.searches += 1
            self.polls = 0
            self.counts = [0] if self.searches == 1 else [3]
            self.resp = [] if self.searches == 1 else [{"username": "a"}] * 3
            return "sid"

    clock = FakeClock()
    fake = Cold([0], [], complete_at=0)
    w = harvest.warm_up(fake, clock=clock, sleep=clock.sleep)
    assert w["attempts"] == 2 and w["responses"] == 3


def test_warm_up_waits_for_a_parent_and_gives_up_when_nothing_answers():
    class NoParent(FakeSlskd):
        def version(self):
            return {"distributedNetwork": {"hasParent": False}}

    clock = FakeClock()
    with pytest.raises(RuntimeError, match="no responses"):
        harvest.warm_up(
            NoParent([0], [], complete_at=0), max_wait=10, sleep=clock.sleep
        )
