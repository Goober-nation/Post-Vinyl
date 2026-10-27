"""Run every query in a plan against slskd and record everything (#34, #74).

Standalone on purpose — stdlib + ``requests`` only — so it runs in a
throwaway container next to a dedicated slskd instance, not inside the app.

For each query it records, in ``<out>/raw/<qid>.json.gz``:

- the query, and every (seed, shape) in the plan that produced it
- a poll trace: (elapsed, responseCount, fileCount) at every poll
- slskd's final search metadata, its search id, and why the search stopped
  (``complete`` | ``stable`` | ``response_cap`` | ``timeout``)
- response and file counts, and per peer: free slot, queue length, upload
  speed, file counts (downloadability is logged now, scored after the run)
- the full, unmodified response list from slskd (kept locally, gitignored,
  so a matcher fix is a re-score, never a re-search)

It does **no matching or filtering**: labelling and scoring run offline on
these files, so a bug in match code is fixed by re-scoring, never by
re-searching. Each query's file is written atomically as soon as it finishes
and a query whose file exists is skipped, so a crash or Ctrl-C loses at most
the search in flight. Each run's settings and slskd version go to
``<out>/runs.jsonl``.

Stop rule (decided 2026-10-26, same as production): the file count unchanged
for 8 s, polling every 2 s, never before 5 s; hard stop at 30 s; 600-response
cap. A stopped search is cancelled so slskd flushes its responses. Before the
first query, and every ``--canary-every`` saved queries, a canary search
("2020", normally 600+ responses) must get 300+ responses: the Soulseek
server throttles an account that searches too often and results go quietly
near-empty. On a failed canary the results since the last good one move to
``suspect/`` (kept) and are searched again once a canary passes (checked
every ``--cooldown`` seconds). Canaries are logged to ``canaries.jsonl``.
Before every search it waits until slskd is logged in **and** has a parent,
and a result is discarded when either is gone afterwards: logged in without
a parent, searches return near-empty results that look real. Searches
run one at a time, back to back; a 409/429 from slskd backs off and retries.

    python harvest.py --plan plan.json --out data/corpus_v2 \\
        --slskd http://slskd-corpus:5030
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

FORMAT_VERSION = 2


class Throttled(RuntimeError):
    """slskd refused a call because it's busy or rate-limiting (409/429)."""


class Slskd:
    def __init__(self, base_url: str, api_key: str, session: Any = None) -> None:
        self.base = base_url.rstrip("/") + "/api/v0"
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def _check(self, resp: Any, what: str) -> Any:
        if resp.status_code in (409, 429):
            raise Throttled(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"{what}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else None

    def server_state(self) -> dict:
        return self._check(
            self.session.get(f"{self.base}/server", timeout=10), "server"
        )

    def version(self) -> Any:
        resp = self.session.get(f"{self.base}/application", timeout=10)
        return resp.json() if resp.status_code == 200 else None

    def start(self, query: str, response_limit: int, timeout_ms: int) -> str:
        body: dict[str, Any] = {
            "searchText": query,
            "responseLimit": response_limit,
            "searchTimeout": timeout_ms,
            # slskd's own default (100) would silently truncate a peer's
            # file list; keep everything a peer sends.
            "fileLimit": 100000,
        }
        data = self._check(
            self.session.post(f"{self.base}/searches", json=body, timeout=15), "start"
        )
        return data["id"]

    def meta(self, search_id: str) -> dict:
        return self._check(
            self.session.get(f"{self.base}/searches/{search_id}", timeout=10), "meta"
        )

    def cancel(self, search_id: str) -> None:
        self.session.put(f"{self.base}/searches/{search_id}", timeout=10)

    def responses(self, search_id: str) -> list:
        return self._check(
            self.session.get(f"{self.base}/searches/{search_id}/responses", timeout=30),
            "responses",
        )


def wait_logged_in(slskd: Slskd, max_wait: float, sleep: Any = time.sleep) -> dict:
    """Block until slskd is connected and logged in to Soulseek.

    A search fired while disconnected returns nothing and would be recorded
    as a real zero-result search — exactly the silent corruption the
    research must not have.
    """
    deadline = time.monotonic() + max_wait
    while True:
        try:
            state = slskd.server_state()
            if state.get("isConnected") and state.get("isLoggedIn"):
                return state
        except (requests.RequestException, RuntimeError) as e:
            state = {"error": str(e)}
        if time.monotonic() >= deadline:
            raise RuntimeError(f"slskd not logged in after {max_wait}s: {state}")
        sleep(5)


def has_parent(slskd: Slskd) -> bool:
    """Whether slskd has a distributed-network parent.

    Logged in without a parent, searches reach almost nobody: live
    2026-10-27, ~110 queries in a row saved 0–1 peers ("Animal", "Duvet")
    while slskd stayed logged in.
    """
    try:
        app = slskd.version() or {}
    except (requests.RequestException, RuntimeError):
        return False
    return bool((app.get("distributedNetwork") or {}).get("hasParent"))


def wait_ready(slskd: Slskd, max_wait: float, sleep: Any = time.sleep) -> dict:
    """Logged in **and** with a network parent, or raise after ``max_wait``.

    The watchdog restarts slskd-corpus when the parent stays lost, so a long
    wait usually ends with a fresh connection.
    """
    state = wait_logged_in(slskd, max_wait=max_wait, sleep=sleep)
    waited = 0.0
    while not has_parent(slskd):
        if waited >= max_wait:
            raise RuntimeError(f"no distributed-network parent after {max_wait}s")
        sleep(5)
        waited += 5
        state = wait_logged_in(slskd, max_wait=max_wait, sleep=sleep)
    return state


CANARY_QUERY = "2020"  # the most-answered word tested (811 responses, 2026-10-27)


def canary(
    slskd: Slskd,
    log: Path,
    *,
    query: str = CANARY_QUERY,
    min_responses: int = 300,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> bool:
    """One very popular search: does slskd still get normal answers?

    Logged in with a parent is not enough. The Soulseek server throttles an
    account that searches too often, and results go quietly near-empty
    (live 2026-10-27: "44" got 36 responses on the test account vs 581 on
    another one). A healthy "2020" fills the 600 cap. Every canary is
    appended to ``log``.
    """
    result = run_search(slskd, query, clock=clock, sleep=sleep)
    ok = result["response_count"] >= min_responses
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "query": query,
        "responses": result["response_count"],
        "ok": ok,
        "search_id": result["search_id"],
    }
    with log.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return ok


def wait_healthy(
    slskd: Slskd,
    log: Path,
    *,
    cooldown: float = 900,
    max_wait: float = 8 * 3600,
    min_responses: int = 300,
    sleep: Any = time.sleep,
) -> None:
    """Canary until it passes, ``cooldown`` seconds apart; raise after ``max_wait``."""
    waited = 0.0
    while True:
        wait_ready(slskd, max_wait=900, sleep=sleep)
        if canary(slskd, log, min_responses=min_responses, sleep=sleep):
            return
        if waited >= max_wait:
            raise RuntimeError(f"canary still failing after {max_wait}s")
        print(f"  canary failed, cooling down {cooldown:.0f}s", flush=True)
        sleep(cooldown)
        waited += cooldown


def run_search(
    slskd: Slskd,
    query: str,
    *,
    response_limit: int = 600,
    poll: float = 2,
    stable: float = 8,
    min_wait: float = 5,
    max_wait: float = 30,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict:
    """Drive one search to its end and return the full record (minus plan data).

    Stop rule (decided 2026-10-26, same as production): stop when the file
    count hasn't changed for ``stable`` seconds, polling every ``poll``
    seconds, never before ``min_wait``; hard stop at ``max_wait``; stop at
    ``response_limit`` responses. slskd finishing on its own also ends it.
    """
    started_at = datetime.now(UTC).isoformat()
    t0 = clock()
    search_id = slskd.start(query, response_limit, int(max_wait * 1000) + 5000)
    trace: list[list[float]] = []
    stop_reason = None
    meta: dict = {}
    last_files, last_change = -1, 0.0
    while True:
        elapsed = clock() - t0
        meta = slskd.meta(search_id)
        count, files = meta.get("responseCount", 0), meta.get("fileCount", 0)
        trace.append([round(elapsed, 2), count, files])
        if files != last_files:
            last_files, last_change = files, elapsed
        if meta.get("isComplete"):
            # slskd ends a search itself at the cap ("ResponseLimitReached").
            capped = "ResponseLimitReached" in str(meta.get("state", ""))
            stop_reason = stop_reason or ("response_cap" if capped else "complete")
            break
        if stop_reason is None:
            if count >= response_limit:
                stop_reason = "response_cap"
            elif elapsed >= max_wait:
                stop_reason = "timeout"
            elif elapsed >= min_wait and elapsed - last_change >= stable:
                stop_reason = "stable"
            if stop_reason:
                slskd.cancel(search_id)
        elif elapsed >= max_wait + 8:
            break  # cancelled but never finalised; fetch what is there
        sleep(poll if stop_reason is None else 0.5)

    # Responses can lag the cancel by a moment; poll briefly when the
    # metadata says there are some but the list is still empty.
    responses = slskd.responses(search_id)
    flush_deadline = clock() + 5
    while (
        not responses and meta.get("responseCount", 0) > 0 and clock() < flush_deadline
    ):
        sleep(0.5)
        responses = slskd.responses(search_id)

    return {
        "search_id": search_id,
        "started_at": started_at,
        "duration_s": round(clock() - t0, 2),
        "stop_reason": stop_reason,
        "meta": meta,
        "trace": trace,
        "response_count": len(responses),
        "file_count": sum(len(r.get("files", [])) for r in responses),
        "peers": [peer_summary(r) for r in responses],
        "responses": responses,
        # slskd's responseCount routinely runs a few above the list it
        # returns (seen live 2026-10-03: 300 vs 296), so a difference is
        # recorded for study, not treated as failure. Only an empty list
        # despite a non-zero count means the flush never landed.
        "response_count_mismatch": len(responses) != meta.get("responseCount", 0),
        "flush_failed": not responses and meta.get("responseCount", 0) > 0,
    }


def peer_summary(response: dict) -> dict:
    """Downloadability per peer: logged now, scored only after the run."""
    return {
        "username": response.get("username"),
        "free_slot": response.get("hasFreeUploadSlot"),
        "queue_length": response.get("queueLength"),
        "upload_speed": response.get("uploadSpeed"),
        "files": len(response.get("files", [])),
        "locked_files": len(response.get("lockedFiles", [])),
    }


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written file


def main(argv: list[str] | None = None, slskd: Slskd | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--slskd", default=os.environ.get("SLSKD_URL", "http://slskd-corpus:5030")
    )
    ap.add_argument(
        "--per-minute", type=float, default=0, help="0 = back to back (default)"
    )
    ap.add_argument("--response-limit", type=int, default=600)
    ap.add_argument("--poll", type=float, default=2)
    ap.add_argument("--stable", type=float, default=8)
    ap.add_argument("--min-wait", type=float, default=5)
    ap.add_argument("--max-wait", type=float, default=30)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument(
        "--canary-every", type=int, default=20, help="0 = only before the run"
    )
    ap.add_argument("--cooldown", type=float, default=900, help="after a failed canary")
    ap.add_argument("--canary-min", type=int, default=300, help="responses to pass")
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text())
    slskd = slskd or Slskd(args.slskd, os.environ["SLSKD_API_KEY"])
    raw_dir = args.out / "raw"
    args.out.mkdir(parents=True, exist_ok=True)

    canaries = args.out / "canaries.jsonl"
    state = wait_logged_in(slskd, max_wait=300)
    wait_healthy(
        slskd,
        canaries,
        cooldown=args.cooldown,
        min_responses=args.canary_min,
        sleep=time.sleep,
    )
    run = {
        "format": FORMAT_VERSION,
        "started_at": datetime.now(UTC).isoformat(),
        "plan": str(args.plan),
        "queries": len(plan["queries"]),
        "per_minute": args.per_minute,
        "response_limit": args.response_limit,
        "poll": args.poll,
        "stable": args.stable,
        "min_wait": args.min_wait,
        "max_wait": args.max_wait,
        "canary_every": args.canary_every,
        "slskd_version": slskd.version(),
        "server_state": state,
    }
    with (args.out / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(run) + "\n")
    (args.out / "seeds.json").write_text(json.dumps(plan["seeds"], ensure_ascii=False))

    interval = 60.0 / args.per_minute if args.per_minute else 0.0
    last_start = 0.0
    # Results saved since the last passing canary. When a canary fails they
    # may have been searched while throttled: they move to suspect/ (kept,
    # never deleted) and the outer loop searches them again.
    since_canary: list[Path] = []
    gave_up: set[str] = set()  # not retried in later passes; a rerun retries them
    while True:
        todo = [
            q
            for q in plan["queries"]
            if q["qid"] not in gave_up
            and not (raw_dir / f"{q['qid']}.json.gz").exists()
        ]
        if not todo:
            break
        print(f"{len(todo)} of {len(plan['queries'])} queries to run", flush=True)
        restart = False
        for n, q in enumerate(todo, 1):
            for attempt in range(1, args.retries + 2):
                time.sleep(max(0.0, last_start + interval - time.monotonic()))
                last_start = time.monotonic()
                try:
                    wait_ready(slskd, max_wait=900)
                    result = run_search(
                        slskd,
                        q["query"],
                        response_limit=args.response_limit,
                        poll=args.poll,
                        stable=args.stable,
                        min_wait=args.min_wait,
                        max_wait=args.max_wait,
                    )
                    # A disconnect mid-search yields a truncated or empty list
                    # that looks real (live 2026-10-03: 'first' saved 0 peers
                    # while slskd was dropping its connection) — discard it.
                    after = slskd.server_state()
                    if not (after.get("isConnected") and after.get("isLoggedIn")):
                        raise RuntimeError(f"connection lost during search: {after}")
                    # Same for the network parent: without one the result is
                    # near-empty but looks real.
                    if not has_parent(slskd):
                        raise RuntimeError("network parent lost during search")
                    result["server_state_after"] = after
                    result["has_parent_after"] = True
                except Throttled as e:
                    # Back off and retry: never push slskd past what it accepts.
                    wait = 30 * attempt
                    print(
                        f"  throttled ({e}), waiting {wait}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                except (requests.RequestException, RuntimeError, KeyError) as e:
                    print(
                        f"  attempt {attempt} failed: {e}", file=sys.stderr, flush=True
                    )
                    continue
                # Peers answered but no list came back: retry rather than
                # record a lost flush as if it were a real zero-result search.
                if result["flush_failed"] and attempt <= args.retries:
                    print(
                        f"  attempt {attempt}: responses never flushed, retrying",
                        flush=True,
                    )
                    continue
                record = {"format": FORMAT_VERSION, "attempt": attempt, **q, **result}
                path = raw_dir / f"{q['qid']}.json.gz"
                save(path, record)
                since_canary.append(path)
                print(
                    f"[{n}/{len(todo)}] {result['stop_reason']:<12} "
                    f"{result['response_count']:>3} peers "
                    f"{result['file_count']:>5} files  {q['query']!r}",
                    flush=True,
                )
                break
            else:
                gave_up.add(q["qid"])
                print(
                    f"[{n}/{len(todo)}] GAVE UP {q['query']!r}",
                    file=sys.stderr,
                    flush=True,
                )
            if args.canary_every and len(since_canary) >= args.canary_every:
                time.sleep(max(0.0, last_start + interval - time.monotonic()))
                last_start = time.monotonic()
                wait_ready(slskd, max_wait=900)
                if canary(slskd, canaries, min_responses=args.canary_min):
                    since_canary = []
                    continue
                suspect = (
                    args.out / "suspect" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                )
                suspect.mkdir(parents=True, exist_ok=True)
                for p in since_canary:
                    os.replace(p, suspect / p.name)
                print(
                    f"  canary failed: {len(since_canary)} results moved to {suspect}, "
                    "redoing them once it passes",
                    flush=True,
                )
                since_canary = []
                wait_healthy(
                    slskd,
                    canaries,
                    cooldown=args.cooldown,
                    min_responses=args.canary_min,
                    sleep=time.sleep,
                )
                restart = True
                break
        if not restart:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
