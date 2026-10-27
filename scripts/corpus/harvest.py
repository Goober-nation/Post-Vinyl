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
first query the runner waits for a distributed-network parent and a warm-up
search that returns responses (fresh logins can answer nothing at first). Searches
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


def warm_up(
    slskd: Slskd,
    *,
    query: str = "love",
    max_wait: float = 180,
    tries: int = 4,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict:
    """Wait until searches really return results before the plan starts.

    Right after login slskd can be logged in yet get no answers (live
    2026-10-27: the first three queries of a run returned 0 responses; the
    same query found 4 peers on another instance minutes later). So wait for
    a distributed-network parent, then run a throwaway search that must
    return responses. The result goes into runs.jsonl, never into raw/.
    """
    waited = 0.0
    while waited < max_wait:
        app = slskd.version() or {}
        if (app.get("distributedNetwork") or {}).get("hasParent"):
            break
        sleep(5)
        waited += 5
    for attempt in range(1, tries + 1):
        result = run_search(slskd, query, clock=clock, sleep=sleep)
        if result["response_count"] > 0:
            return {
                "parent_wait_s": waited,
                "attempts": attempt,
                "query": query,
                "responses": result["response_count"],
            }
        sleep(30)
    raise RuntimeError(f"warm-up search {query!r} got no responses in {tries} tries")


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
    args = ap.parse_args(argv)

    plan = json.loads(args.plan.read_text())
    slskd = slskd or Slskd(args.slskd, os.environ["SLSKD_API_KEY"])
    raw_dir = args.out / "raw"
    args.out.mkdir(parents=True, exist_ok=True)

    state = wait_logged_in(slskd, max_wait=300)
    warm = warm_up(slskd, sleep=time.sleep)
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
        "slskd_version": slskd.version(),
        "server_state": state,
        "warm_up": warm,
    }
    with (args.out / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(run) + "\n")
    (args.out / "seeds.json").write_text(json.dumps(plan["seeds"], ensure_ascii=False))

    interval = 60.0 / args.per_minute if args.per_minute else 0.0
    todo = [
        q for q in plan["queries"] if not (raw_dir / f"{q['qid']}.json.gz").exists()
    ]
    print(f"{len(todo)} of {len(plan['queries'])} queries to run", flush=True)
    last_start = 0.0
    for n, q in enumerate(todo, 1):
        for attempt in range(1, args.retries + 2):
            time.sleep(max(0.0, last_start + interval - time.monotonic()))
            last_start = time.monotonic()
            try:
                wait_logged_in(slskd, max_wait=600)
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
                result["server_state_after"] = after
            except Throttled as e:
                # Back off and retry: never push slskd past what it accepts.
                wait = 30 * attempt
                print(
                    f"  throttled ({e}), waiting {wait}s", file=sys.stderr, flush=True
                )
                time.sleep(wait)
                continue
            except (requests.RequestException, RuntimeError, KeyError) as e:
                print(f"  attempt {attempt} failed: {e}", file=sys.stderr, flush=True)
                continue
            # Peers answered but no list came back: retry rather than record
            # a lost flush as if it were a real zero-result search.
            if result["flush_failed"] and attempt <= args.retries:
                print(
                    f"  attempt {attempt}: responses never flushed, retrying",
                    flush=True,
                )
                continue
            record = {"format": FORMAT_VERSION, "attempt": attempt, **q, **result}
            save(raw_dir / f"{q['qid']}.json.gz", record)
            print(
                f"[{n}/{len(todo)}] {result['stop_reason']:<12} "
                f"{result['response_count']:>3} peers {result['file_count']:>5} files"
                f"  {q['query']!r}",
                flush=True,
            )
            break
        else:
            print(
                f"[{n}/{len(todo)}] GAVE UP {q['query']!r}", file=sys.stderr, flush=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
