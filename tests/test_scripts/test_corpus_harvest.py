"""Tests for the standalone harvester against a fake slskd (#34)."""

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

    def __init__(self, counts, responses, complete_at=None, empty_fetches=0):
        self.counts = list(counts)
        self.resp = responses
        self.complete_at = complete_at
        self.empty_fetches = empty_fetches
        self.cancelled = False
        self.polls = 0
        self.started = []

    def server_state(self):
        return {"isConnected": True, "isLoggedIn": True}

    def version(self):
        return {"version": "test"}

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
            "fileCount": self.counts[i] * 2,
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
        response_limit=kw.get("limit", 300),
        max_wait=kw.get("max_wait", 10),
        clock=clock,
        sleep=clock.sleep,
    )


def test_natural_completion_is_not_cancelled():
    fake = FakeSlskd([0, 3, 5], [{"username": "a"}] * 5, complete_at=2)
    r = _run(fake)
    assert r["stop_reason"] == "complete" and not fake.cancelled
    assert [t[1] for t in r["trace"]] == [0, 3, 5]


def test_no_early_stop_below_cap_runs_to_deadline():
    # 50 responses at 1s would satisfy the app's threshold; research must keep going.
    fake = FakeSlskd([50] * 100, [{"username": "a"}] * 50)
    r = _run(fake)
    assert r["stop_reason"] == "deadline"
    assert r["trace"][-2][0] >= 10


def test_response_cap_cancels():
    fake = FakeSlskd([10, 300], [{"username": "a"}] * 300)
    r = _run(fake)
    assert r["stop_reason"] == "response_cap" and fake.cancelled
    assert not r["response_count_mismatch"]


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

    assert fake.started == [("Air", 300)]  # q2 already on disk → skipped
    with gzip.open(out / "raw" / "q1.json.gz", "rt") as fh:
        rec = json.load(fh)
    assert rec["query"] == "Air" and rec["uses"] == [{"seed": "s1", "shape": "A"}]
    assert rec["responses"] == resp and rec["stop_reason"] == "complete"
    assert rec["meta"]["responseCount"] == 1 and rec["trace"]
    run = json.loads((out / "runs.jsonl").read_text().splitlines()[0])
    assert run["response_limit"] == 300 and run["slskd_version"] == {"version": "test"}
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

    fake = Drops([0], [], complete_at=0)
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
